"""The GitHub client against scripted answers: redirects, caps, malformed payloads."""
from __future__ import annotations

import pytest

from custom_components.private_hacs import github
from custom_components.private_hacs.github import GitHubClient, GitHubError

SHA = "a" * 40
CODELOAD = "https://codeload.github.com/acme/demo/legacy.tar.gz/abc?token=x"


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _Resp:
    def __init__(self, status=200, headers=None, body=b"", data=None) -> None:
        self.status, self.headers, self.content, self._data = status, headers or {}, _Content(body), data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _Session:
    def __init__(self, *responses) -> None:
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def client(*responses):
    session = _Session(*responses)
    return GitHubClient(session, "secret-token"), session


async def test_tarball_redirect_to_codeload_is_followed_without_the_token():
    c, s = client(_Resp(302, {"Location": CODELOAD}), _Resp(200, body=b"tar-bytes"))
    assert await c.download_tarball("acme/demo", SHA) == b"tar-bytes"
    api_call, codeload_call = s.calls
    assert api_call[1]["headers"]["Authorization"] == "Bearer secret-token"
    assert "Authorization" not in codeload_call[1]["headers"]
    assert codeload_call[0].startswith("https://codeload.github.com/")
    assert api_call[1]["allow_redirects"] is False and codeload_call[1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/a.tgz",
        "https://raw.githubusercontent.com/evil/x/main/a.tgz",  # a GitHub host, but not codeload
        "https://api.github.com/x",
        "https://codeload.github.com.evil.example/a.tgz",
        "https://evil.example\\@codeload.github.com/a.tgz",
        "https://user:pw@codeload.github.com/a.tgz",
        "http://codeload.github.com/a.tgz",
        "//codeload.github.com/a.tgz",
        "file:///etc/passwd",
        "",
    ],
)
async def test_redirects_anywhere_else_are_refused(location):
    c, s = client(_Resp(302, {"Location": location}))
    with pytest.raises(GitHubError, match="Refusing redirect"):
        await c.download_tarball("acme/demo", SHA)
    assert len(s.calls) == 1  # nothing was requested from the bad target


@pytest.mark.parametrize("status", [202, 204, 206])
async def test_only_a_plain_200_is_a_tarball(status):
    c, _ = client(_Resp(status, body=b"x"))
    with pytest.raises(GitHubError, match="Unexpected status"):
        await c.download_tarball("acme/demo", SHA)


async def test_download_is_capped(monkeypatch):
    monkeypatch.setattr(github, "MAX_DOWNLOAD_BYTES", 100)
    c, _ = client(_Resp(302, {"Location": CODELOAD}), _Resp(200, body=b"x" * 101))
    with pytest.raises(GitHubError, match="exceeds"):
        await c.download_tarball("acme/demo", SHA)


@pytest.mark.parametrize("status,error", [(401, "refused the request"), (403, "refused the request"), (404, "Not found"), (500, "error 500")])
async def test_http_errors_are_clean_errors(status, error):
    c, _ = client(_Resp(status, data={}))
    with pytest.raises(GitHubError, match=error):
        await c.get_repo("acme/demo")


@pytest.mark.parametrize("bad", ["../x", "a/b/c", "a/", "/b", "a b/c", "a/b\n", "٣/b"])
async def test_repository_names_are_checked_before_any_request(bad):
    c, s = client()
    for call in (c.get_repo, c.list_tag_names):
        with pytest.raises(GitHubError, match="Invalid repository"):
            await call(bad)
    assert s.calls == []


@pytest.mark.parametrize("bad", ["main", "v1.2", "v1.2.3\n", "../v1.0.0", "v1.0.0/../x"])
async def test_tag_names_are_checked_before_any_request(bad):
    c, s = client()
    with pytest.raises(GitHubError, match="Invalid tag"):
        await c.get_tag_ref("acme/demo", bad)
    assert s.calls == []


@pytest.mark.parametrize("bad", ["abc", "A" * 40, "a" * 39, "a" * 40 + "\n", "../" + "a" * 37])
async def test_object_ids_are_checked_before_any_request(bad):
    c, s = client()
    with pytest.raises(GitHubError, match="Invalid"):
        await c.get_tag_object("acme/demo", bad)
    with pytest.raises(GitHubError, match="Invalid"):
        await c.download_tarball("acme/demo", bad)
    assert s.calls == []


async def test_tag_list_keeps_only_release_tags_and_follows_pages():
    page1 = [{"name": f"v1.{i}.0"} for i in range(100)]
    page2 = [{"name": "v2.0.0"}, {"name": "nightly"}, {"name": "v01.0.0"}, {"nope": 1}]
    c, s = client(_Resp(data=page1), _Resp(data=page2))
    names = await c.list_tag_names("acme/demo")
    assert len(names) == 101 and "v2.0.0" in names and "nightly" not in names
    assert len(s.calls) == 2


async def test_a_huge_tag_list_is_an_error_not_a_silent_truncation():
    full_page = [{"name": f"v1.{i}.0"} for i in range(100)]
    c, _ = client(*[_Resp(data=full_page) for _ in range(github.MAX_TAG_PAGES)])
    with pytest.raises(GitHubError, match="more than"):
        await c.list_tag_names("acme/demo")


@pytest.mark.parametrize("payload", [None, "text", 5, {"a": 1}])
async def test_unexpected_payload_shapes_are_clean_errors(payload):
    c, _ = client(_Resp(data=payload))
    with pytest.raises(GitHubError):
        await c.list_tag_names("acme/demo")


async def test_a_missing_tag_is_none_not_an_error():
    c, _ = client(_Resp(404, data={}))
    assert await c.get_tag_ref("acme/demo", "v1.0.0") is None


@pytest.mark.parametrize(
    "location",
    ["https://codeload.github.com:8443/a.tgz", "https://codeload.github.com:22/a.tgz"],
)
async def test_a_redirect_to_another_port_is_refused(location):
    c, s = client(_Resp(302, {"Location": location}))
    with pytest.raises(GitHubError, match="Refusing redirect"):
        await c.download_tarball("acme/demo", SHA)
    assert len(s.calls) == 1


async def test_the_explicit_default_port_is_fine():
    c, _ = client(
        _Resp(302, {"Location": "https://codeload.github.com:443/a.tgz"}), _Resp(200, body=b"ok")
    )
    assert await c.download_tarball("acme/demo", SHA) == b"ok"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_an_api_redirect_explains_itself_and_is_not_followed(status):
    c, s = client(_Resp(status, {"Location": "https://api.github.com/repositories/1"}))
    with pytest.raises(GitHubError, match="renamed or moved"):
        await c.get_repo("acme/demo")
    assert len(s.calls) == 1


@pytest.mark.parametrize("bad", ["a/.", "a/..", "acme/.."])
async def test_dot_repository_names_are_refused_before_any_request(bad):
    c, s = client()
    with pytest.raises(GitHubError, match="Invalid repository"):
        await c.get_repo(bad)
    assert s.calls == []


async def test_repository_names_that_start_with_a_dot_are_fine():
    c, _ = client(_Resp(data={"full_name": "acme/.github"}))
    assert (await c.get_repo("acme/.github"))["full_name"] == "acme/.github"
