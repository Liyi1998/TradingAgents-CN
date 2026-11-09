"""Pure public-fetch boundary validation, with no substituted network client."""
import pytest

from app.data.sources.cn.shared.news_channels import is_public_sina_url


@pytest.mark.parametrize("url", [
    "http://finance.sina.com.cn/a.html", "https://finance.sina.com.cn:8443/a.html",
    "https://finance.sina.com.cn:invalid/a.html", "https://user:private@finance.sina.com.cn/a.html",
    "https://finance.sina.com.cn.internal/a.html", "https://192.168.1.17/a.html",
    "https://127.0.0.1/a.html", "file:///private/a.html", "https://[::1]/a.html",
])
def test_untrusted_links_are_rejected_without_a_request(url):
    assert not is_public_sina_url(url)


def test_only_named_public_sina_surfaces_are_allowed():
    assert is_public_sina_url("https://finance.sina.com.cn/roll/a.shtml")
    assert is_public_sina_url("https://cj.sina.cn/articles/view/a")
    listing = "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php?symbol=sh600977"
    assert not is_public_sina_url(listing)
    assert is_public_sina_url(listing, include_archive=True)
