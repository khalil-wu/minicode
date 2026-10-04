from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
import subprocess
import sys

import pytest

from backend.agent import instruction_discovery as discovery
from backend.agent.context import ContextBuilder
from backend.agent.loop_session import populate_prompt_context
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.ws.handlers.misc import handle_interrupt_command


@pytest.fixture
def instruction_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, '_get_managed_minicode_dir', lambda: tmp_path / 'managed')
    monkeypatch.setattr(discovery, 'get_minicode_config_home_dir', lambda: tmp_path / 'user')
    discovery.clear_guideline_cache()
    yield tmp_path
    discovery.clear_guideline_cache()


def test_projectless_instructions_do_not_borrow_process_workspace(instruction_roots, monkeypatch):
    root = instruction_roots
    (root / '.git').mkdir()
    (root / 'AGENTS.md').write_text('PRIVATE_PROJECT_GUIDANCE', encoding='utf-8')
    (root / 'user').mkdir()
    (root / 'user' / 'INSTRUCTIONS.md').write_text('PERSONAL_GUIDANCE', encoding='utf-8')
    monkeypatch.chdir(root)
    bundle = discovery.load_project_guideline_bundle(workspace_dir=None)
    assert 'PERSONAL_GUIDANCE' in bundle.rendered_markdown
    assert 'PRIVATE_PROJECT_GUIDANCE' not in bundle.rendered_markdown
    assert bundle.to_dict()['workspace_dir'] == ''


def test_configured_fallback_is_loaded_from_project_directory(instruction_roots):
    root = instruction_roots
    (root / '.git').mkdir()
    (root / 'TEAM.md').write_text('TEAM_FALLBACK_GUIDANCE', encoding='utf-8')
    bundle = discovery.load_project_guideline_bundle(root, project_doc_fallback_filenames=['TEAM.md'])
    assert 'TEAM_FALLBACK_GUIDANCE' in bundle.rendered_markdown


def test_windows_utf8_bom_keeps_skill_discovery_and_conditional_rule_scope(instruction_roots, monkeypatch):
    root = instruction_roots
    (root / '.git').mkdir()
    skill = root / 'skills' / 'windows'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: windows\ndescription: Windows workflow.\n---\nWINDOWS_SKILL_BODY', encoding='utf-8-sig')
    loader = SkillLoader()
    monkeypatch.setattr(loader, '_search_dirs', lambda: [('workspace', root / 'skills')])
    manager = SkillManager(loader)
    assert manager.detect('$windows')[0].name == 'windows'
    assert 'WINDOWS_SKILL_BODY' in manager.load_skill_payload('windows')['content']
    rules = root / '.minicode' / 'rules'
    rules.mkdir(parents=True)
    (rules / 'python.md').write_text('---\npaths: ["*.py"]\n---\nWINDOWS_PYTHON_RULE', encoding='utf-8-sig')
    assert 'WINDOWS_PYTHON_RULE' not in discovery.load_project_guidelines(root)
    assert 'WINDOWS_PYTHON_RULE' in discovery.load_matching_project_rules(root, ['main.py'])


def test_active_editor_file_loads_scoped_instructions_before_any_tool(instruction_roots):
    root = instruction_roots
    (root / '.git').mkdir()
    (root / 'src' / '.minicode' / 'rules').mkdir(parents=True)
    target = root / 'src' / 'main.py'
    target.write_text('value = 1\n', encoding='utf-8')
    (root / 'AGENTS.md').write_text('ROOT_GUIDANCE', encoding='utf-8')
    (root / 'src' / 'AGENTS.md').write_text('SCOPED_GUIDANCE', encoding='utf-8')
    (root / 'src' / '.minicode' / 'rules' / 'python.md').write_text('---\npaths: ["*.py"]\n---\nPYTHON_GUIDANCE', encoding='utf-8')
    state = AgentState(user_message='Fix this file', workspace_root=root)
    populate_prompt_context(state=state, metadata={'primary_file': str(target)}, workspace_root=root, permission_context=PermissionContext(mode='confirm'))
    builder = ContextBuilder(workspace_root=root)
    instructions = builder._build_prompt_parts(state, root).render_user_instructions()
    assert instructions.index('ROOT_GUIDANCE') < instructions.index('SCOPED_GUIDANCE')
    assert 'PYTHON_GUIDANCE' in instructions
    assert f'Scope: {root / "src"}' in instructions
    assert str(target) in builder._build_environment_context_xml(state)
    populate_prompt_context(state=state, metadata={}, workspace_root=root, permission_context=PermissionContext(mode='confirm'))
    assert str(target) not in builder._build_environment_context_xml(state)


@pytest.mark.parametrize('role', ['explore', 'plan'])
def test_child_role_matches_its_model_visible_execution_mode(role, instruction_roots):
    state = AgentState(user_message='Delegate')
    populate_prompt_context(state=state, metadata={'agent_mode': 'subagent', 'agent_role': f'subagent:{role}'}, workspace_root=instruction_roots, permission_context=PermissionContext(mode='plan'))
    assert f'mode: {role}' in ContextBuilder._build_agent_mode_block(state)


def test_explicit_skill_tail_survives_initial_load_and_checkpoint_roundtrip(instruction_roots):
    content = 'workflow\n' * 4000 + 'REQUIRED_FINAL_INSTRUCTION'
    state = AgentState(user_message='Use the selected workflow')
    state.prompt_context['skill_injections'] = [{'name': 'large', 'path': str(instruction_roots / 'SKILL.md'), 'content': content}]
    builder = ContextBuilder()
    fragments = builder._consume_skill_injections(state)
    assert 'REQUIRED_FINAL_INSTRUCTION' in fragments[0]
    snapshot = builder.export_snapshot()
    restored = ContextBuilder()
    restored.load_snapshot(snapshot)
    assert restored.export_snapshot()['invoked_skills'][0]['content'] == content


def test_exact_selected_skill_does_not_also_activate_same_named_fallback(instruction_roots, monkeypatch):
    for directory, marker in [('user-skills', 'USER_WORKFLOW'), ('project-skills', 'PROJECT_WORKFLOW')]:
        skill_dir = instruction_roots / directory / 'shared'
        skill_dir.mkdir(parents=True)
        (skill_dir / 'SKILL.md').write_text(f'---\nname: shared\ndescription: A workflow.\n---\n{marker}', encoding='utf-8')
    loader = SkillLoader()
    monkeypatch.setattr(loader, '_search_dirs', lambda: [('user', instruction_roots / 'user-skills'), ('workspace', instruction_roots / 'project-skills')])
    manager = SkillManager(loader)
    selected = instruction_roots / 'project-skills' / 'shared' / 'SKILL.md'
    detections = manager.detect('$shared', selected_skills=[{'name': 'shared', 'path': str(selected)}])
    assert len(detections) == 1
    assert detections[0].source_path == str(selected)
    assert 'PROJECT_WORKFLOW' in manager.load_skill_payload(detections[0].name, source_path=detections[0].source_path)['content']


def test_missing_selected_skill_does_not_switch_to_same_named_workflow(instruction_roots, monkeypatch):
    skill_dir = instruction_roots / 'shared'
    skill_dir.mkdir()
    (skill_dir / 'SKILL.md').write_text('---\nname: shared\ndescription: A workflow.\n---\nUNSELECTED_WORKFLOW', encoding='utf-8')
    loader = SkillLoader()
    monkeypatch.setattr(loader, '_search_dirs', lambda: [('user', instruction_roots)])
    manager = SkillManager(loader)
    missing = instruction_roots / 'removed' / 'SKILL.md'
    detections = manager.detect('$shared', selected_skills=[{'name': 'shared', 'path': str(missing)}])
    assert len(detections) == 1
    assert manager.load_skill_payload('shared', source_path=detections[0].source_path) is None


@pytest.mark.asyncio
async def test_interrupt_replay_cannot_stop_a_different_turn():
    session = SimpleNamespace(
        active_conversation_id='conversation',
        _conversation_streams={'conversation': {'turn_id': 'new-turn', 'message_id': 'new-message'}},
        run_manager=SimpleNamespace(
            run_task_ids={'conversation': 'new-task'},
            set_user_queue_paused=lambda *args: None,
            queued_user_message_snapshot=lambda conversation_id: [],
        ),
        command_dispatcher=SimpleNamespace(interrupt_user_message_admission=AsyncMock(return_value=False)),
        cancel_agent_runs=AsyncMock(),
    )
    await handle_interrupt_command(session, {'conversation_id': 'conversation', 'message_id': 'old-message'})
    await handle_interrupt_command(session, {'conversation_id': 'conversation'})
    session.cancel_agent_runs.assert_not_awaited()
    await handle_interrupt_command(session, {'conversation_id': 'conversation', 'message_id': 'new-message'})
    session.cancel_agent_runs.assert_awaited_once_with(conversation_id='conversation', reason='user_interrupted')


def test_resume_rejects_a_checkpoint_replaced_after_selection(monkeypatch):
    from backend.agent import query_recovery
    monkeypatch.setattr(query_recovery, 'load_latest_checkpoint', lambda *args, **kwargs: SimpleNamespace(run_id='replacement'))
    context = ContextBuilder()
    with pytest.raises(ValueError, match='no longer current'):
        prepare_query_recovery(session_id='session', conversation_id='conversation', metadata={'resume_from_checkpoint': True, 'resume_checkpoint_run_id': 'selected'}, state=AgentState(user_message='resume'), context_builder=context, max_iterations_budget=10, current_run_id='new-run')
    assert context.history_length == 0


def test_scheduler_registry_belongs_to_the_selected_state_root(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-c', 'from backend.tasks.scheduler import SCHEDULE_FILE, SCHEDULE_REGISTRY_FILE; print(SCHEDULE_FILE); print(SCHEDULE_REGISTRY_FILE)'], cwd=repo, env={**os.environ, 'MINICODE_STATE_ROOT': str(tmp_path), 'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True, encoding="utf-8", check=True)
    assert result.stdout.splitlines() == [str(tmp_path / '.minicode' / 'scheduled_tasks.json'), str(tmp_path / '.minicode' / 'scheduled_task_projects.json')]
