"""Private, atomic configuration for the independent Jev shadow checker."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from app.llm.jev_conditions import CONFIG_PATH, ConditionConfig, load_condition_config
from app.llm.jev_review import CONFIG_PATH as REVIEW_PATH, load_config


class ConditionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool = Field(strict=True)
    external_calls_enabled: bool = Field(strict=True)
    max_calls_per_day: int = Field(ge=1, le=200, strict=True)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_external: bool = Field(default=False, strict=True)
    decision_experiments_enabled: bool | None = Field(default=None, strict=True)
    final_action_enabled: bool | None = Field(default=None, strict=True)


class SettingsConflict(Exception):
    pass


class SettingsRejected(Exception):
    pass


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separate lock inode survives atomic replacement; never silently proceed unlocked.
    with open(path.with_suffix(".lock"), "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"absent").hexdigest()


def _available_key(config: ConditionConfig, review_path: Path):
    if config.api_key.get_secret_value().strip():
        return config.api_key, "conditions"
    try:
        legacy = load_config(review_path)
        if legacy.api_key.get_secret_value().strip():
            return legacy.api_key, "saved_review"
    except Exception:
        # A broken legacy config must not prevent disabling this independent feature.
        pass
    return None, "missing"


def _public(config: ConditionConfig, path: Path, review_path: Path) -> dict:
    _, source = _available_key(config, review_path)
    return {"enabled": config.enabled, "external_calls_enabled": config.external_calls_enabled,
            "decision_experiments_enabled": config.decision_experiments_enabled,
            "final_action_enabled": config.final_action_enabled,
            "max_calls_per_day": config.max_calls_per_day, "model": config.model,
            "timeout_seconds": config.timeout_seconds, "mode": "shadow",
            "key_available": source != "missing", "key_source": source,
            "revision": _revision(path)}


def get_condition_settings(path: Path = CONFIG_PATH, review_path: Path = REVIEW_PATH) -> dict:
    with _locked(path):
        return _public(load_condition_config(path), path, review_path)


def update_condition_settings(update: ConditionSettingsUpdate, path: Path = CONFIG_PATH,
                              review_path: Path = REVIEW_PATH) -> dict:
    with _locked(path):
        if update.revision != _revision(path):
            raise SettingsConflict("配置已被其他操作修改，请重新读取后再保存")
        config = load_condition_config(path)
        if update.final_action_enabled is True and not config.final_action_enabled and not update.confirm_external:
            raise SettingsRejected("启用自动动作建议前须确认：后续新报告可自动外发一次，替代旧条件核验，不覆盖原裁决")
        if update.decision_experiments_enabled is True and not config.decision_experiments_enabled and not update.confirm_external:
            raise SettingsRejected("启用动作试验前须确认公共策略及日线数据外发范围；每次试验仍需单独确认")
        if update.external_calls_enabled and not update.enabled:
            raise SettingsRejected("请先启用条件核验，或同时关闭外发")
        if update.external_calls_enabled:
            if not (config.enabled and config.external_calls_enabled) and not update.confirm_external:
                raise SettingsRejected("启用外发前必须确认数据范围和可能产生的用量")
            key, _ = _available_key(config, review_path)
            if key is None:
                raise SettingsRejected("未找到可复用的 Jev 密钥，请由管理员配置")
            config.api_key = key
        config.enabled = update.enabled
        config.external_calls_enabled = update.external_calls_enabled
        config.max_calls_per_day = update.max_calls_per_day
        if update.decision_experiments_enabled is not None:
            config.decision_experiments_enabled = update.decision_experiments_enabled
        if update.final_action_enabled is not None:
            config.final_action_enabled = update.final_action_enabled
        # SecretStr.model_dump_json masks keys: serialize the secret only into the private file.
        value = config.model_dump(mode="json")
        value["api_key"] = config.api_key.get_secret_value()
        ConditionConfig.model_validate(value)
        fd, name = tempfile.mkstemp(prefix=".jev-conditions-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return _public(config, path, review_path)
