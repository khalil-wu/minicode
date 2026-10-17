/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { handleSessionEvent } from "../chat/sessionEvents";
import { composerWorkspaceKey } from "./composer-drafts";
import type { CommandResultEvent } from "../protocol/events";
import { latestProjectionCommandIds } from "../protocol/command-projection";
import { clearDiffCommentScope, getDiffCommentScope, readDiffCommentState, setDiffComments } from "../lib/diff-comment-storage";
import type { DiffComment } from "../lib/diff-comments";

vi.mock("../protocol/ws-outbox",()=>({sendClientCommand:vi.fn(()=>true),sendClientCommandAwaitResult:vi.fn(),commandResultSucceeded:(result:{level:string})=>result.level==="success",sendConversationDeleteCommand:vi.fn()}));
const buffers = {textStreamBuffer:{destroy:vi.fn()},thinkingStreamBuffer:{destroy:vi.fn()}} as never;
const switched = (id:string,root="C:/project",commandId?:string,commandType?:string)=>handleSessionEvent({type:"conversation.switched",conversation_id:id,client_command_id:commandId,client_command_type:commandType,conversation:{id,title:id,updated_at:"2026-10-12",workspace_root:root,messages:[]}} as never,buffers);
beforeEach(()=>{
  vi.clearAllMocks();
  latestProjectionCommandIds.clear();
  clearDiffCommentScope(getDiffCommentScope(null, "C:/project"));
  useAppStore.setState({conversationId:null,pendingConversationSwitchId:null,pendingConversationCreateId:null,pendingComposerConversationAdoption:null,
    conversationInventoryInstanceId:null,conversationInventoryRevision:0,conversations:[],messages:[],conversationMessages:{},conversationStreaming:{},
    conversationWorkbenchStates:{},workspaceComposerDrafts:{},conversationAgentStates:{},workingDirectory:"C:/project",appMode:"cowork",
    draft:"Keep homepage draft",attachments:[{id:"local",name:"notes.txt",type:"text/plain",size:5,status:"error",error:"retry available"}],
    selectedMentions:[{kind:"folder",name:"docs",path:"C:/project/docs"}],selectedSkills:[{kind:"skill",name:"writing",path:"C:/skills/writing/SKILL.md"}],
    quotedMessage:null,allowedRemoteImageDomains:["images.example"],editorTabs:[]});
});

it("moves workspace comments only when their exact create/adopt owner is acknowledged",async()=>{
  const comment: DiffComment = { type: "comment", content: [{ content_type: "text", text: "keep exact original comment" }],
    position: { path: "src/app.ts", side: "left", line: 8 }, localWorkspaceRoot: "C:/project" };
  const scope = getDiffCommentScope(null, "C:/project");
  setDiffComments(scope, [comment]);
  let complete!:(value:CommandResultEvent)=>void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve;}));
  const ensure = useAppStore.getState().ensureComposerConversation();
  const id = useAppStore.getState().pendingConversationCreateId!;
  expect(readDiffCommentState(scope).comments).toEqual([comment]);
  switched(id);
  complete({type:"command.result",command:"conversation.create",level:"success",message:"",data:{conversation_id:id}});
  expect(await ensure).toBe(id);
  expect(readDiffCommentState(id).comments).toEqual([comment]);
  expect(readDiffCommentState(scope).comments).toEqual([]);
});

it("adopts the current workspace composer through the authoritative created conversation",async()=>{
  let complete!:(value:CommandResultEvent)=>void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve;}));
  const ensure = useAppStore.getState().ensureComposerConversation();
  const id = useAppStore.getState().pendingConversationCreateId!;
  expect(id).toBeTruthy();
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({type:"conversation.create",workspace_root:"C:/project",conversation_id:id}),"conversation.create");
  useAppStore.getState().setDraft("Keep typing while creating");
  switched(id);
  complete({type:"command.result",command:"conversation.create",level:"success",message:"",data:{conversation_id:id}});
  expect(await ensure).toBe(id);
  expect(useAppStore.getState()).toMatchObject({conversationId:id,draft:"Keep typing while creating",attachments:[{id:"local"}],selectedMentions:[{name:"docs"}],selectedSkills:[{name:"writing"}],allowedRemoteImageDomains:["images.example"]});
  expect(useAppStore.getState().conversationWorkbenchStates[id].draft).toBe("Keep typing while creating");
  expect(useAppStore.getState().workspaceComposerDrafts[composerWorkspaceKey("C:/project")]).toBeUndefined();
});

it("retains a failed initial creation's full workspace draft and returns no goal owner",async()=>{
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({type:"command.result",command:"conversation.create",level:"error",message:"Create failed",data:{}} as never);
  expect(await useAppStore.getState().ensureComposerConversation()).toBeNull();
  expect(useAppStore.getState()).toMatchObject({conversationId:null,pendingConversationSwitchId:null,pendingComposerConversationAdoption:null,draft:"Keep homepage draft",attachments:[{id:"local"}],selectedSkills:[{name:"writing"}]});
});

it("does not adopt a homepage draft into a newer selected task or apply a goal after navigation",async()=>{
  let complete!:(value:CommandResultEvent)=>void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve;}));
  const ensure = useAppStore.getState().ensureComposerConversation();
  const createdId = useAppStore.getState().pendingConversationCreateId!;
  const createCommandId = String(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0].client_command_id);
  useAppStore.getState().requestConversationSwitch("other-task");
  const switchCommandId = String(vi.mocked(sendClientCommand).mock.calls.find(([command])=>command.type==="conversation.switch")![0].client_command_id);
  switched("other-task","C:/other",switchCommandId,"conversation.switch");
  switched(createdId,"C:/project",createCommandId,"conversation.create");
  complete({type:"command.result",command:"conversation.create",level:"success",message:"",data:{conversation_id:createdId}});
  expect(await ensure).toBeNull();
  expect(useAppStore.getState()).toMatchObject({conversationId:"other-task",draft:""});
  expect(useAppStore.getState().workspaceComposerDrafts[composerWorkspaceKey("C:/project")].draft).toBe("Keep homepage draft");
  expect(useAppStore.getState().conversationWorkbenchStates[createdId]).toBeUndefined();
});

it("uses an already active task without creating or moving its composer",async()=>{
  useAppStore.setState({conversationId:"existing"});
  expect(await useAppStore.getState().ensureComposerConversation()).toBe("existing");
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("Keep homepage draft");
});

it("keeps an adopted homepage draft in its real isolated workspace",async()=>{
  const root = "C:/project/.minicode/worktrees/conv_real_owner";
  useAppStore.setState({workingDirectory:root});
  let complete!:(value:CommandResultEvent)=>void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve;}));
  const ensure = useAppStore.getState().ensureComposerConversation();
  const id = useAppStore.getState().pendingConversationCreateId!;
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({workspace_root:root}),"conversation.create");
  switched(id,root);
  complete({type:"command.result",command:"conversation.create",level:"success",message:"",data:{conversation_id:id}});
  expect(await ensure).toBe(id);
  expect(useAppStore.getState()).toMatchObject({workingDirectory:root,draft:"Keep homepage draft"});
});
