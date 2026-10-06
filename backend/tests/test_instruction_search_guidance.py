from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.prompting import PromptParts, build_stable_prompt, build_tool_runtime_guidance
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import search_tools
from backend.tools.search_tools import GrepFilesTool


def test_injected_instruction_content_is_marked_as_already_loaded():
    text = PromptParts(stable='system', project_guidelines='Scope: src\nKeep public API unchanged').render_user_instructions()
    assert 'already been loaded by MiniCode' in text
    assert 'listed sources and scopes' in text
    assert 'Keep public API unchanged' in text
    assert PromptParts(stable='system').render_user_instructions() == ''


def test_official_base_does_not_add_minicode_instruction_file_conventions():
    prompt = build_stable_prompt()
    assert '.minicode/INSTRUCTIONS.md' not in prompt
    assert 'Do not blindly probe or create an instruction file' not in prompt
    assert '`INSTRUCTIONS.md` is where' not in prompt


def test_absent_instruction_file_is_not_created_or_presented_as_loaded(tmp_path, monkeypatch):
    from backend.agent import instruction_discovery

    monkeypatch.setattr(instruction_discovery, '_get_managed_minicode_dir', lambda: tmp_path / 'managed')
    monkeypatch.setattr(instruction_discovery, 'get_minicode_config_home_dir', lambda: tmp_path / 'user')
    instruction_discovery.clear_guideline_cache()
    state = AgentState(user_message='Inspect the existing files', workspace_root=tmp_path)
    builder = ContextBuilder(workspace_root=tmp_path)
    parts = builder._build_prompt_parts(state, tmp_path)
    assert parts.render_user_instructions() == ''
    assert not (tmp_path / '.minicode' / 'INSTRUCTIONS.md').exists()


def test_literal_search_is_visible_to_direct_and_code_mode_callers():
    tool = GrepFilesTool()
    for schema in [tool.model_schema(), tool.get_schema()]:
        assert schema.parameters['properties']['fixed_strings']['type'] == 'boolean'
        assert 'exact text' in schema.parameters['properties']['pattern']['description']
    assert 'fixed_strings=true' in tool.model_description()
    assert 'fixed_strings=true' in build_tool_runtime_guidance([{'function': {'name': 'grep_files'}}])


@pytest.fixture(params=['ripgrep', 'python'])
def search_backend(request, monkeypatch):
    monkeypatch.setattr(search_tools, '_HAS_RIPGREP', request.param == 'ripgrep')
    return request.param


@pytest.mark.asyncio
@pytest.mark.parametrize('snippet', ['fetch(', 'eval(', 'addEventListener(', '[a-z]+', 'fetch(|eval('])
async def test_literal_source_snippets_do_not_acquire_regex_meaning(tmp_path, search_backend, snippet):
    target = tmp_path / 'source.js'
    target.write_bytes((f'const exact = {snippet}\nconst unrelated = fetchx\n').encode('utf-8'))
    context = ToolExecutionContext(permission=PermissionContext(mode='bypass'), workspace_root=tmp_path)
    result = await GrepFilesTool().execute({'pattern': snippet, 'fixed_strings': True, 'output_mode': 'content'}, context=context)
    assert not result.is_error
    assert f'const exact = {snippet}' in result.content
    assert 'const unrelated = fetchx' not in result.content


@pytest.mark.asyncio
async def test_invalid_regex_remains_an_error_with_actionable_feedback(tmp_path, search_backend):
    (tmp_path / 'source.js').write_text('fetch(url); eval(code);\n', encoding='utf-8')
    context = ToolExecutionContext(permission=PermissionContext(mode='bypass'), workspace_root=tmp_path)
    tool = GrepFilesTool()
    failed = await tool.execute({'pattern': 'https?://|fetch(|innerHTML|eval(|localStorage|addEventListener'}, context=context)
    assert failed.is_error
    assert failed.error_kind == 'validation_error'
    assert failed.recoverable
    assert r'fetch\(' in failed.content
    assert 'fixed_strings=true' in failed.content
    corrected = await tool.execute({'pattern': r'fetch\(|eval\(', 'output_mode': 'content'}, context=context)
    assert not corrected.is_error
    assert 'fetch(url); eval(code);' in corrected.content


@pytest.mark.asyncio
async def test_literal_search_preserves_filters_case_context_and_pagination(tmp_path, search_backend):
    (tmp_path / 'source.js').write_text('before\nFETCH(first)\nbetween\nfetch(second)\nafter\n', encoding='utf-8')
    (tmp_path / 'other.py').write_text('fetch(hidden)\n', encoding='utf-8')
    context = ToolExecutionContext(permission=PermissionContext(mode='bypass'), workspace_root=tmp_path)
    result = await GrepFilesTool().execute({'pattern': 'fetch(', 'fixed_strings': True, 'case_insensitive': True, 'glob': '*.js', 'output_mode': 'content', 'offset': 1, 'head_limit': 1}, context=context)
    assert not result.is_error
    assert 'fetch(second)' in result.content
    assert 'FETCH(first)' not in result.content
    assert 'fetch(hidden)' not in result.content
    with_context = await GrepFilesTool().execute({'pattern': 'FETCH(', 'fixed_strings': True, 'output_mode': 'content', 'context': 1}, context=context)
    assert not with_context.is_error
    assert 'before' in with_context.content
    assert 'between' in with_context.content


@pytest.mark.asyncio
async def test_literal_search_keeps_file_permission_filtering(tmp_path, search_backend):
    allowed, denied = tmp_path / 'allowed.js', tmp_path / 'denied.js'
    allowed.write_text('fetch(allowed)\n', encoding='utf-8')
    denied.write_text('fetch(secret)\n', encoding='utf-8')
    checker = SimpleNamespace(prepare_path_check=lambda **_kwargs: lambda path: Path(path).name != 'denied.js')
    context = ToolExecutionContext(permission=PermissionContext(mode='confirm'), workspace_root=tmp_path, permission_checker=checker)
    result = await GrepFilesTool().execute({'pattern': 'fetch(', 'fixed_strings': True, 'output_mode': 'content'}, context=context)
    assert not result.is_error
    assert 'fetch(allowed)' in result.content
    assert 'fetch(secret)' not in result.content
