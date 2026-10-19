"""Policy evidence comes from real files, including local edits before exporting commits."""
import json
import subprocess

import pytest

from decompnet.forks import policy
from decompnet.network import NetworkError
from decompnet.upstream import export
from .test_phase23 import net  # noqa: F401


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env={'PATH': '/usr/bin:/bin', 'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                               'GIT_COMMITTER_NAME': 'test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}).stdout


def test_singular_doc_llm_policy_and_case_variants_take_priority(tmp_path):
    (tmp_path / 'doc').mkdir()
    (tmp_path / 'doc/llm_policy.md').write_text('Contributions made with llms are allowed after human review.\n')
    (tmp_path / 'AGENTS.md').write_text('Never commit AI-generated code without reviewing it.\n')
    (tmp_path / 'README.md').write_text('# AI DISCLOSURE\n\nChatGPT assistance must be disclosed.\n')
    git(tmp_path, 'init', '-q'); git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'Initial rules')
    result = policy.check(str(tmp_path))
    assert result['error'] is None and result['lean'] == 'rules'
    assert result['files'][0] == 'AGENTS.md' and 'doc/llm_policy.md' in result['files']
    assert len(result['revision']) == 40 and len(result['fingerprint']) == 64
    assert {line['file'] for line in result['lines']} == {'AGENTS.md', 'README.md', 'doc/llm_policy.md'}
    before = result['fingerprint']
    (tmp_path / 'doc/llm_policy.md').write_text('Do not submit code made with LLMs.\n')
    local = policy.inspect(tmp_path, working_tree=True)
    assert local['fingerprint'] != before
    assert policy.inspect(tmp_path)['fingerprint'] == before


def test_oversized_and_external_links_are_reported_without_reading_them(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'AI_POLICY.md').write_bytes(b'A' * (policy.MAX_FILE_BYTES + 1))
    outside = tmp_path / 'outside'; outside.write_text('AI must not see this private sentinel.')
    (root / 'AGENTS.md').symlink_to(outside)
    got = policy.inspect(root, working_tree=True)
    assert got['error'] and len(got['skipped']) == 2 and not got['lines']
    assert 'private sentinel' not in json.dumps(got)
    assert policy.lines_about_ai('The mail said there was a chair.') == []


def test_upstream_requires_actual_policy_review_before_any_writes(net, tmp_path):  # noqa: F811
    repo = tmp_path / 'destination'; repo.mkdir()
    original = '# Rules\n\nAI-assisted contributions are allowed after review and disclosure.\n'
    (repo / 'README.md').write_text(original)
    (repo / 'doc').mkdir()
    (repo / 'doc/llm_policy.md').write_text('LLM output must be reviewed by a person.\n')
    with pytest.raises(NetworkError, match=r'doc/llm_policy.md.*LLM'):
        export(net, 'demo', repo, log=lambda _: None)
    assert not (repo / '.git').exists() and (repo / 'README.md').read_text() == original
    assert export(net, 'demo', repo, log=lambda _: None, acknowledge_policy=True) == 0
    assert (repo / 'README.md').read_text() == original
    assert policy.NOTICE_MARKER in (repo / 'DECOMPWTF.md').read_text()


def test_generated_notice_does_not_trigger_its_own_review_but_added_rules_do(net, tmp_path):  # noqa: F811
    repo = tmp_path / 'fresh'
    assert export(net, 'demo', repo, log=lambda _: None) == 0
    assert export(net, 'demo', repo, log=lambda _: None) == 0
    with (repo / 'README.md').open('a') as stream:
        stream.write('\n\nAI contributions must include human review.\n')
    with pytest.raises(NetworkError, match='human review'):
        export(net, 'demo', repo, log=lambda _: None)


def test_clones_and_room_members_keep_independently_read_evidence(tmp_path):
    from decompnet.app.engine import RomNetwork
    from decompnet.app.forkroom import register_member
    repo = tmp_path / 'upstream'; repo.mkdir()
    (repo / 'AGENTS.md').write_text('AI-assisted contributions require human review.\n')
    (repo / 'objdiff.json').write_text('{"units":[]}')
    git(repo, 'init', '-q'); git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'Rules')
    work = tmp_path / 'work'; work.mkdir()
    host = RomNetwork(tmp_path / 'host.db', work)
    slug = host.forks.clone(str(repo), platform='psx', log=lambda _: None)
    proj = host._project(slug)
    saved = host.cfg(proj)['contribution_policy']
    assert saved['lines'][0]['file'] == 'AGENTS.md'
    root = __import__('pathlib').Path(proj['root'])
    (root / 'doc').mkdir()
    (root / 'doc/llm_policy.md').write_text('LLM output must include disclosure.\n')
    updated = host.forks.review_policy(slug)
    assert updated['fingerprint'] != saved['fingerprint']
    assert host.cfg(host._project(slug))['contribution_policy'] == updated
    # A member reads its own tree, never the host's supplied claim about what the policy says.
    guest = RomNetwork(tmp_path / 'guest.db', tmp_path / 'guest-work')
    cfg = register_member(guest, 'member', root, {'title': 'Synthetic PS1', 'platform': 'psx',
                          'contribution_policy': {'lines': [], 'lean': 'yes'}}, 'host')
    assert cfg['contribution_policy']['lines'] and cfg['contribution_policy']['lean'] != 'yes'


def test_acknowledging_appended_rules_preserves_them(net, tmp_path):  # noqa: F811
    repo = tmp_path / 'fresh'
    export(net, 'demo', repo, log=lambda _: None)
    with (repo / 'README.md').open('a') as stream:
        stream.write('\n\nDo not submit AI-generated code without disclosure.\n')
    before = (repo / 'README.md').read_text()
    export(net, 'demo', repo, log=lambda _: None, acknowledge_policy=True)
    assert (repo / 'README.md').read_text() == before
