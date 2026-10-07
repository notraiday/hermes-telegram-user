import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


PACKAGE_DIRS = ("core", "scripts")


def _package_sources() -> list[Path]:
    """Python files shipped with the plugin: root modules plus core/ and scripts/.

    Deliberately an allowlist rather than a denylist: tests/, study material under
    other/ and caches can never leak into the compile and read-only contract checks.
    """
    sources = list(ROOT.glob("*.py"))
    for name in PACKAGE_DIRS:
        base = ROOT / name
        if base.is_dir():
            sources.extend(base.rglob("*.py"))
    return sorted(p for p in sources if "__pycache__" not in p.relative_to(ROOT).parts)


def test_required_files_exist():
    for name in (
        "plugin.yaml",
        "core/accounts.py",
        "tools.py",
        "core/archive.py",
        "core/client.py",
        "core/collections.py",
        "core/folders.py",
        "core/helpers.py",
        "core/limits.py",
        "core/media.py",
        "core/readstate.py",
        "core/sanitize.py",
        "core/state/aliases.py",
        "core/state/archive.py",
        "core/state/collections.py",
        "core/state/paths.py",
        "core/state/transcripts.py",
        "core/state/watermarks.py",
        "scripts/setup_session.py",
        "README.md",
    ):
        assert (ROOT / name).exists()


def test_no_core_patch_files():
    assert not (ROOT / "gateway").exists()
    assert not (ROOT / "agent").exists()


def test_python_sources_compile():
    for path in _package_sources():
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_current_hermes_tool_contract_is_present():
    source = (ROOT / "tools.py").read_text(encoding="utf-8")
    assert "schema={" in source
    assert '"name": name' in source
    assert '"description": description' in source
    assert '"parameters": schema_params' in source
    assert "is_async=True" in source
    assert "check_fn=_check_requirements" in source
    assert "return json.dumps(" in source


def test_there_is_no_chat_platform():
    assert not (ROOT / "adapter.py").exists()
    for path in _package_sources():
        assert "register_platform(" not in path.read_text(encoding="utf-8"), path


def test_tool_surface_matches_manifest():
    """plugin.yaml is the published contract; tools.py must register exactly that set."""
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    source = (ROOT / "tools.py").read_text(encoding="utf-8")
    assert 'version: "0.12.0+rai.4"' in manifest
    published = [
        line.strip()[2:] for line in manifest.splitlines() if line.strip().startswith("- tg_")
    ]
    assert len(published) == 46
    assert len(set(published)) == 46
    row_names = re.findall(r'^ {8}"(tg_[a-z_]+)",$', source, re.MULTILINE)
    registered = set(row_names)
    assert registered == set(published), (
        f"manifest-only: {sorted(set(published) - registered)}; "
        f"code-only: {sorted(registered - set(published))}"
    )
    # A name must appear exactly once per row. A stray duplicate means the row is
    # malformed — e.g. a bare string where the handler belongs, which still read
    # as the right *set* of names and so slipped past the check above.
    assert len(row_names) == len(published), (
        f"{len(row_names)} tool-name lines for {len(published)} tools: a row is malformed"
    )
    for tool in (
        "tg_get_message_context",
        "tg_search_global",
        "tg_list_folders",
        "tg_get_unread",
        "tg_read_folder",
        "tg_search_media",
        "tg_download_media",
        "tg_transcribe_voice",
        "tg_contacts",
        "tg_participants",
        "tg_set_alias",
        "tg_list_aliases",
        "tg_delete_alias",
        "tg_get_pinned",
        "tg_get_drafts",
        "tg_get_scheduled",
        "tg_get_profile",
        "tg_get_sessions",
        "tg_archive_sync",
        "tg_archive_search",
        "tg_archive_status",
        "tg_archive_forget",
        "tg_list_digest_marks",
        "tg_forget_digest_marks",
        "tg_mark_summarized",
        "tg_save_collection",
        "tg_list_collections",
        "tg_set_collection_brief",
        "tg_delete_collection",
        "tg_read_collection",
    ):
        assert tool in published


WRITE_TOOLS = (
    "_tg_send_message",
    "_tg_send_file",
    "_tg_forward_messages",
    "_tg_send_reaction",
    "_tg_edit_message",
    "_tg_delete_messages",
    "_tg_pin_message",
    "_tg_mark_read",
)


def test_every_telegram_write_is_gated_by_account_mode():
    """Writes exist now, but only on accounts with MODE=write.

    Every write handler must call require_write() before it does anything, and the
    read acknowledgement still lives in exactly one module.
    """
    tools_source = (ROOT / "tools.py").read_text(encoding="utf-8")
    for name in WRITE_TOOLS:
        start = tools_source.index(f"async def {name}(")
        end = tools_source.index("\nasync def ", start + 10) if "\nasync def " in tools_source[start + 10:] else len(tools_source)
        body = tools_source[start:end]
        assert "require_write()" in body, f"{name} writes without checking the account mode"
        first_client = body.find("tool_client()")
        assert body.find("require_write()") < first_client, f"{name} connects before the mode check"

    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in _package_sources()
    }
    for name, source in sources.items():
        assert "send_read_acknowledge(" not in source, f"{name} acknowledges reads the invisible way"
    acknowledging = sorted(
        name
        for name, source in sources.items()
        if any(m in source for m in ("ReadHistoryRequest(", "ReadDiscussionRequest(",
                                      "ReadMentionsRequest(", "ReadReactionsRequest("))
    )
    assert acknowledging == ["core/readstate.py"], acknowledging
    # tg_mark_summarized only for writable accounts, plus tg_mark_read.
    assert tools_source.count("acknowledge_read(") == 2
    assert 'get_account().writable' in tools_source


def test_floodwait_and_single_instance_guards_are_wired():
    limits = (ROOT / "core" / "limits.py").read_text(encoding="utf-8")
    client = (ROOT / "core" / "client.py").read_text(encoding="utf-8")
    assert "TelegramRateLimited" in limits
    assert "note_flood_wait" in limits
    assert "threading.BoundedSemaphore" in limits
    assert "flood_sleep_threshold=configured_flood_sleep_threshold()" in client
    assert "async with tool_gate()" in client


def test_voice_cache_uses_native_hermes_stt():
    tools = (ROOT / "tools.py").read_text(encoding="utf-8")
    transcripts = (ROOT / "core" / "state" / "transcripts.py").read_text(encoding="utf-8")
    assert "from tools.transcription_tools import transcribe_audio" in tools
    assert "tg_transcribe_voice" in tools
    assert "CREATE TABLE IF NOT EXISTS transcripts" in transcripts
    assert "PRIMARY KEY (chat_id, message_id)" in transcripts


def test_sanitizer_strips_bidi_override_but_keeps_emoji_joiners():
    namespace = {}
    source = (ROOT / "core" / "sanitize.py").read_text(encoding="utf-8")
    exec(compile(source, "core/sanitize.py", "exec"), namespace)
    sanitize_text = namespace["sanitize_text"]
    assert sanitize_text("a\u202eb") == "ab"
    assert sanitize_text("👨‍💻") == "👨‍💻"
