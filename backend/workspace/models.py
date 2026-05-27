from __future__ import annotations

from pydantic import BaseModel, Field


class WorkspaceTreeEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None = None
    modified_at: str
    has_children: bool = False


class WorkspaceTreeResponse(BaseModel):
    workspace_root: str
    requested_path: str
    entries: list[WorkspaceTreeEntry] = Field(default_factory=list)


class WorkspaceSearchResultResponse(BaseModel):
    path: str
    name: str
    score: float
    matched_indices: list[int] = Field(default_factory=list)
    kind: str = "file"


class WorkspaceSearchResponse(BaseModel):
    query: str
    results: list[WorkspaceSearchResultResponse] = Field(default_factory=list)


class WorkspaceFileResponse(BaseModel):
    workspace_root: str
    path: str
    name: str
    content: str
    content_hash: str
    size_bytes: int
    modified_at: str
    language_hint: str


class WorkspaceFileUpdateRequest(BaseModel):
    path: str = Field(min_length=1)
    content: str


class WorkspaceFileCompareWriteRequest(BaseModel):
    path: str = Field(min_length=1)
    expected_hash: str = ""
    content: str


class WorkspaceDirectoryCreateRequest(BaseModel):
    path: str = Field(min_length=1)


class WorkspacePathRenameRequest(BaseModel):
    path: str = Field(min_length=1)
    new_path: str = Field(min_length=1)


class WorkspacePathResponse(BaseModel):
    workspace_root: str
    path: str
    name: str
    is_dir: bool
    size_bytes: int | None = None
    modified_at: str | None = None


class WorkspaceDeleteResponse(BaseModel):
    workspace_root: str
    path: str
    deleted: bool = True
    is_dir: bool


class WorkspaceGitWorktreeEntryResponse(BaseModel):
    path: str
    branch: str
    commit: str
    is_main: bool = False
    is_current: bool = False
    is_detached: bool = False
    is_isolated: bool = False
    can_remove: bool = False


class WorkspaceGitWorktreeResponse(BaseModel):
    is_worktree: bool = False
    current_path: str = ""
    main_repo_path: str | None = None
    current_branch: str | None = None
    common_git_dir: str | None = None
    worktree_count: int = 0
    worktrees: list[WorkspaceGitWorktreeEntryResponse] = Field(default_factory=list)
    error: str | None = None


class WorkspaceGitWorktreeSwitchRequest(BaseModel):
    path: str = Field(min_length=1)


class WorkspaceGitWorktreeRemoveResponse(BaseModel):
    removed: bool
    path: str
    branch: str = ""
    error: str | None = None


class ProjectImportRequest(BaseModel):
    """项目导入请求"""
    path: str = Field(min_length=1, description="项目根目录路径")


class ProjectImportResponse(BaseModel):
    """项目导入响应"""
    success: bool
    project: dict = Field(default_factory=dict, description="项目元数据")
    summary: str = Field(default="", description="项目摘要（用于 system prompt）")
    file_count: int = Field(default=0, description="文件数量")


class WorkspaceMetadata(BaseModel):
    """工作区元信息（产品级）"""
    path: str
    name: str
    project_type: str | None = None  # python, node, rust, go, etc.
    is_git_repo: bool = False
    is_worktree: bool = False
    main_repo_path: str | None = None
    current_branch: str | None = None
    last_accessed: str | None = None  # ISO 8601 timestamp


class FileTreeNode(BaseModel):
    """文件树节点（产品级）"""
    name: str
    path: str
    type: str  # "file" | "directory"
    size: int | None = None
    modified: str | None = None  # ISO 8601 timestamp
    git_status: str | None = None  # "modified", "added", "deleted", "untracked", etc.
    children: list["FileTreeNode"] = Field(default_factory=list)
    is_expanded: bool = False


class GitStatus(BaseModel):
    """Git 状态（产品级）"""
    current_branch: str
    is_clean: bool
    ahead: int = 0
    behind: int = 0
    staged: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class WorkspaceSnapshot(BaseModel):
    """工作区快照（用于 WebSocket 推送）"""
    metadata: WorkspaceMetadata
    git_status: GitStatus | None = None
    file_count: int = 0
    total_size: int = 0
