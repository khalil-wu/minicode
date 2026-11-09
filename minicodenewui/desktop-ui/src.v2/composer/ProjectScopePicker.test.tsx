/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { activateWorkspaceFolder, openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { isDesktop } from "../desktop/runtime";
import { ProjectScopePicker } from "./ProjectScopePicker";
vi.mock("../workspace/openWorkspaceFolder",()=>({activateWorkspaceFolder:vi.fn(async()=>"C:/selected"),openWorkspaceFolder:vi.fn()}));
vi.mock("../desktop/runtime",async()=>({...await vi.importActual<typeof import("../desktop/runtime")>("../desktop/runtime"),isDesktop:vi.fn(()=>true)}));
const originalCreate = useAppStore.getState().createConversation;
beforeEach(()=>useAppStore.setState({workspaceGit:null,conversations:[],conversationId:"scope-owner",workingDirectory:"C:/first",draft:"Preserve this task",recentWorkspaces:[{path:"C:/first"},{path:"C:/selected"}]}));
afterEach(()=>{cleanup();vi.clearAllMocks();useAppStore.setState({createConversation:originalCreate});});

it("uses the named project's current source after its original identity folder was removed",()=>{
  useAppStore.setState({workingDirectory:"C:/first",recentWorkspaces:[{path:"C:/first",name:"Named research",sourceFolders:["C:/replacement"],named:true}]});
  render(<ProjectScopePicker />);
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  fireEvent.change(screen.getByRole("textbox",{name:"搜索项目"}),{target:{value:"Named research"}});
  fireEvent.click(screen.getByRole("button",{name:"Named research"}));
  expect(activateWorkspaceFolder).toHaveBeenCalledExactlyOnceWith("C:/replacement","cowork");
  expect(useAppStore.getState()).toMatchObject({conversationId:"scope-owner",workingDirectory:"C:/first",draft:"Preserve this task"});
});

it("opens a zero-source project's managed root and keeps its actual source list empty",()=>{
  useAppStore.setState({recentWorkspaces:[{path:"C:/stable-id",name:"Managed research",sourceFolders:[],managedWorkspaceRoot:"C:/app-data/projects/id/workspace",named:true}]});
  render(<ProjectScopePicker />);
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  fireEvent.click(screen.getByRole("button",{name:"Managed research"}));
  expect(activateWorkspaceFolder).toHaveBeenCalledExactlyOnceWith("C:/app-data/projects/id/workspace","cowork");
  expect(useAppStore.getState().recentWorkspaces[0].sourceFolders).toEqual([]);
});

it("retains a selected secondary source instead of switching an already selected project to its first source",()=>{
  useAppStore.setState({workingDirectory:"C:/second",recentWorkspaces:[{path:"C:/first",name:"Multi-source",sourceFolders:["C:/first","C:/second"],named:true}]});
  render(<ProjectScopePicker />);
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  const selected=screen.getByRole("button",{name:"Multi-source"});
  expect(selected.getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(selected);
  expect(activateWorkspaceFolder).not.toHaveBeenCalled();
});
it("selects a registered project through workspace activation without editing the composer",()=>{
  useAppStore.setState({workingDirectory:"C:/first",draft:"Preserve this task",recentWorkspaces:[{path:"C:/first"},{path:"C:/selected"}]});
  render(<ProjectScopePicker />);
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  fireEvent.change(screen.getByRole("textbox",{name:"搜索项目"}),{target:{value:"selected"}});
  fireEvent.click(screen.getByRole("button",{name:"selected"}));
  expect(activateWorkspaceFolder).toHaveBeenCalledWith("C:/selected","cowork");
  expect(useAppStore.getState().draft).toBe("Preserve this task");
});

it("keeps a projectless task's execution directory out of project selection",()=>{
  useAppStore.setState({workingDirectory:"C:/tasks/build-a",conversations:[{
    id:"scope-owner",title:"Task",updatedAt:"2026-10-13",workspaceRoot:"",worktreePath:"",executionCwd:"C:/tasks/build-a",
  }]});
  render(<ProjectScopePicker />);
  expect(screen.getByRole("button",{name:"选择项目"}).textContent).toBe("选择项目");
  expect(screen.queryByText("build-a")).toBeNull();
  expect(screen.queryByText("本地")).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  expect(screen.getByRole("button",{name:"不在项目中工作"}).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button",{name:"first"}).getAttribute("aria-pressed")).toBe("false");
  expect(useAppStore.getState().workingDirectory).toBe("C:/tasks/build-a");
  expect(useAppStore.getState().draft).toBe("Preserve this task");
});

it("opens from the home project title and shows the selected project check",()=>{
  render(<ProjectScopePicker />);
  act(()=>window.dispatchEvent(new CustomEvent("composer:open-project-picker")));
  const current = screen.getByRole("button",{name:"first"});
  expect(current.getAttribute("aria-pressed")).toBe("true");
  expect(current.querySelector(".composer-project-selected")).not.toBeNull();
  fireEvent.keyDown(screen.getByRole("textbox",{name:"搜索项目"}),{key:"ArrowDown"});
  expect(document.activeElement).toBe(current);
  fireEvent.keyDown(current,{key:"Escape"});
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button",{name:"选择项目"}));
});

it("opens named project creation and starts a projectless conversation without editing the original draft",()=>{
  const create = vi.fn(async()=>true);
  useAppStore.setState({startConversationDraft:create});
  render(<ProjectScopePicker />);
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  fireEvent.click(screen.getByRole("button",{name:"新建项目"}));
  expect(useAppStore.getState().projectEditTarget).toBe("create");
  expect(openWorkspaceFolder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"选择项目"}));
  fireEvent.click(screen.getByRole("button",{name:"不在项目中工作"}));
  expect(create).toHaveBeenCalledWith({bindWorkspace:false,appMode:"cowork"});
  expect(useAppStore.getState().draft).toBe("Preserve this task");
});

it("shows only branch metadata for the selected workspace and opens its Git workspace",()=>{
  useAppStore.setState({workspaceGit:{branch:"feature/scope",currentPath:"c:/FIRST",isWorktree:false}});
  render(<ProjectScopePicker />);
  expect(screen.getByText("本地")).toBeTruthy();
  fireEvent.click(screen.getByRole("button",{name:"查看分支 feature/scope"}));
  expect(useAppStore.getState().activeBottomTab).toBe("git");
  act(()=>useAppStore.setState({workspaceGit:{branch:"foreign",currentPath:"C:/other",isWorktree:false}}));
  expect(screen.queryByRole("button",{name:/查看分支/})).toBeNull();
});

it("omits a local desktop claim in a browser and reads matching persisted thread Git metadata",()=>{
  vi.mocked(isDesktop).mockReturnValue(false);
  useAppStore.setState({conversations:[{id:"scope-owner",title:"scope",updatedAt:"2026-10-12",workspaceRoot:"C:/first",gitBranch:"recorded-branch"}]});
  render(<ProjectScopePicker />);
  expect(screen.queryByText("本地")).toBeNull();
  expect(screen.getByRole("button",{name:"查看分支 recorded-branch"})).toBeTruthy();
  act(()=>useAppStore.setState({workingDirectory:""}));
  expect(screen.queryByRole("button",{name:/查看分支/})).toBeNull();
  vi.mocked(isDesktop).mockReturnValue(true);
});
