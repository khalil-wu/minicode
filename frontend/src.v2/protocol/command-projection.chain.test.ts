/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { latestProjectionCommandIds, rememberSentCommandProjection, isSupersededCommandProjection } from "./command-projection";
import { handleSessionEvent } from "../chat/sessionEvents";
import { useAppStore } from "../stores";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { sendClientCommand } from "./ws-outbox";
import type { ClientCommand, ServerEvent } from "./events";
vi.mock("./ws-outbox",()=>({sendClientCommand:vi.fn(()=>true),sendClientCommandAwaitResult:vi.fn(),commandResultSucceeded:()=>true,sendConversationDeleteCommand:vi.fn(),resolveClientCommandResult:vi.fn(()=>false)}));
const buffers = {textStreamBuffer:{destroy:vi.fn()},thinkingStreamBuffer:{destroy:vi.fn()}} as never;
const receipt = (commandId:string,commandType:string,id:string):ServerEvent=>({type:"conversation.switched",client_command_id:commandId,client_command_type:commandType,conversation_id:id,
  conversation:{id,title:id,updated_at:"2026-10-12",workspace_root:"C:/project",messages:[]}} as never);
const send = (command:ClientCommand)=>rememberSentCommandProjection(command,String(command.client_command_id),useAppStore.getState().conversationId);
beforeEach(()=>{
  latestProjectionCommandIds.clear();vi.clearAllMocks();
  useAppStore.setState({conversationId:"original",conversations:[{id:"original",title:"original",updatedAt:"2026-10-12",workspaceRoot:"C:/project"}],workingDirectory:"C:/project",
    messages:[],conversationMessages:{},conversationStreaming:{},conversationAgentStates:{},conversationWorkbenchStates:{},workspaceComposerDrafts:{},
    pendingConversationSwitchId:null,pendingConversationCreateId:null,pendingComposerConversationAdoption:null,draft:"Keep original draft",attachments:[],selectedSkills:[],selectedMentions:[]});
});

it("uses the latest switch receipt even after its spinner and transport ACK have completed",()=>{
  send({type:"conversation.switch",conversation_id:"first",client_command_id:"first-command"});
  send({type:"conversation.switch",conversation_id:"second",client_command_id:"second-command"});
  handleSessionEvent(receipt("second-command","conversation.switch","second"),buffers);
  useAppStore.getState().setDraft("Keep second draft");
  handleSessionEvent(receipt("first-command","conversation.switch","first"),buffers);
  expect(useAppStore.getState()).toMatchObject({conversationId:"second",pendingConversationSwitchId:null,draft:"Keep second draft"});
  expect(useAppStore.getState().conversationWorkbenchStates.original.draft).toBe("Keep original draft");
});

it("rejects a superseded create receipt before and after the newer explicit selection",()=>{
  send({type:"conversation.create",conversation_id:"created",client_command_id:"create-command"});
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"switch-command"});
  expect(isSupersededCommandProjection(receipt("create-command","conversation.create","created"))).toBe(true);
  handleSessionEvent(receipt("switch-command","conversation.switch","chosen"),buffers);
  handleSessionEvent(receipt("create-command","conversation.create","created"),buffers);
  expect(useAppStore.getState().conversationId).toBe("chosen");
});

it("lets the first actual user message create a conversation after the old navigation receipt",()=>{
  send({type:"conversation.switch",conversation_id:"old",client_command_id:"old-command"});
  useAppStore.setState({conversationId:null,conversations:[]});
  send({type:"user_message",content:"Initial task",client_command_id:"first-send"});
  const event = receipt("first-send","user_message","automatic");
  expect(isSupersededCommandProjection(event)).toBe(false);
  handleSessionEvent(event,buffers);
  expect(useAppStore.getState().conversationId).toBe("automatic");
});

it("lets actual project activation and deleting the selected conversation activate their backend-chosen owner",()=>{
  send({type:"conversation.switch",conversation_id:"old",client_command_id:"old-command"});
  send({type:"workspace.set",path:"C:/project",client_command_id:"workspace-command"});
  handleSessionEvent(receipt("workspace-command","workspace.set","project-task"),buffers);
  expect(useAppStore.getState().conversationId).toBe("project-task");
  send({type:"conversation.delete",conversation_id:"project-task",client_command_id:"delete-command"});
  handleSessionEvent(receipt("delete-command","conversation.delete","fallback-task"),buffers);
  expect(useAppStore.getState().conversationId).toBe("fallback-task");
});

it("does not let background task creation, deletion, or ordinary messages replace a pending navigation receipt",()=>{
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"navigation"});
  send({type:"conversation.create",conversation_id:"background",activate:false,client_command_id:"background-create"});
  send({type:"conversation.delete",conversation_id:"unselected",client_command_id:"background-delete"});
  send({type:"user_message",conversation_id:"original",content:"Followup",client_command_id:"ordinary-send"});
  expect(latestProjectionCommandIds.get("active-conversation")).toBe("navigation");
  handleSessionEvent(receipt("navigation","conversation.switch","chosen"),buffers);
  expect(useAppStore.getState().conversationId).toBe("chosen");
});

it("keeps a newer repeated-target switch pending when an older command fails",()=>{
  send({type:"conversation.switch",conversation_id:"target",client_command_id:"first-target"});
  send({type:"conversation.switch",conversation_id:"other",client_command_id:"other-target"});
  send({type:"conversation.switch",conversation_id:"target",client_command_id:"latest-target"});
  useAppStore.setState({pendingConversationSwitchId:"target"});
  handleCommandResultEvent({type:"command.result",command:"conversation.switch",client_command_id:"first-target",level:"error",message:"Original switch failed",data:{conversation_id:"target"}} as never);
  expect(useAppStore.getState().pendingConversationSwitchId).toBe("target");
});

it("activates a fork only from its real switch receipt and preserves the parent draft",()=>{
  send({type:"conversation.switch",conversation_id:"original",client_command_id:"old-switch"});
  send({type:"context.fork",conversation_id:"original",message_id:"answer",create_branch:true,activate:true,client_command_id:"fork"});
  expect(latestProjectionCommandIds.get("active-conversation")).toBe("fork");
  expect(useAppStore.getState().conversationId).toBe("original");
  handleSessionEvent(receipt("fork","context.fork","branch"),buffers);
  expect(useAppStore.getState().conversationId).toBe("branch");
  expect(useAppStore.getState().conversationWorkbenchStates.original.draft).toBe("Keep original draft");
});

it("does not let a delayed fork activation replace a newer selected task",()=>{
  send({type:"context.fork",conversation_id:"original",message_id:"answer",create_branch:true,activate:true,client_command_id:"fork"});
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"chosen-command"});
  handleSessionEvent(receipt("chosen-command","conversation.switch","chosen"),buffers);
  useAppStore.getState().setDraft("Keep chosen draft");
  handleSessionEvent(receipt("fork","context.fork","branch"),buffers);
  expect(useAppStore.getState()).toMatchObject({conversationId:"chosen",draft:"Keep chosen draft"});
});

it("lets a background fork update the list without owning navigation",()=>{
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"chosen-command"});
  send({type:"context.fork",conversation_id:"original",message_id:"answer",create_branch:true,activate:false,client_command_id:"background-fork"});
  expect(latestProjectionCommandIds.get("active-conversation")).toBe("chosen-command");
  expect(latestProjectionCommandIds.get("conversation-list")).toBe("background-fork");
  handleSessionEvent(receipt("chosen-command","conversation.switch","chosen"),buffers);
  expect(useAppStore.getState().conversationId).toBe("chosen");
});

it("uses the real Header archive home receipt and retains the archived conversation draft",()=>{
  useAppStore.setState({appMode:"cowork"});
  send({type:"conversation.archive",conversation_id:"original",archive_navigation:"home",client_command_id:"archive-home"});
  handleSessionEvent({type:"conversation.switched",client_command_id:"archive-home",client_command_type:"conversation.archive",archive_navigation:"home",
    conversation_id:null,conversation:null,is_hydrating:false,session:{active_conversation_id:null}} as never,buffers);
  expect(useAppStore.getState().conversationId).toBeNull();
  expect(useAppStore.getState().appMode).toBe("cowork");
  expect(useAppStore.getState().conversationWorkbenchStates.original.draft).toBe("Keep original draft");
  handleSessionEvent({type:"conversation.list",client_command_id:"archive-home",client_command_type:"conversation.archive",archive_navigation:"home",
    active_conversation_id:null,active_conversation:null,session:{active_conversation_id:null},conversations:[
      {id:"original",title:"original",archived:true,updated_at:"2026-10-13",workspace_root:"C:/project"},
      {id:"other",title:"other",updated_at:"2026-10-13",workspace_root:"C:/project"},
    ]} as never,buffers);
  expect(useAppStore.getState().conversationId).toBeNull();
  expect(useAppStore.getState().appMode).toBe("cowork");
  expect(sendClientCommand).not.toHaveBeenCalledWith({type:"conversation.switch",conversation_id:"other"});
});

it("does not let an older Header archive home replace a later explicit task selection",()=>{
  send({type:"conversation.archive",conversation_id:"original",archive_navigation:"home",client_command_id:"archive-home"});
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"chosen-command"});
  handleSessionEvent(receipt("chosen-command","conversation.switch","chosen"),buffers);
  useAppStore.getState().setDraft("Keep chosen draft");
  handleSessionEvent({type:"conversation.switched",client_command_id:"archive-home",client_command_type:"conversation.archive",archive_navigation:"home",
    conversation_id:null,conversation:null,is_hydrating:false,session:{active_conversation_id:null}} as never,buffers);
  expect(useAppStore.getState()).toMatchObject({conversationId:"chosen",draft:"Keep chosen draft"});
});

it("leaves legacy sidebar archive navigation intact and a background home archive owns only inventory",()=>{
  send({type:"conversation.switch",conversation_id:"chosen",client_command_id:"chosen-command"});
  send({type:"conversation.archive",conversation_id:"unselected",client_command_id:"sidebar-archive"});
  expect(latestProjectionCommandIds.get("active-conversation")).toBe("chosen-command");
  expect(isSupersededCommandProjection(receipt("sidebar-archive","conversation.archive","sidebar-fallback"))).toBe(false);
  send({type:"conversation.archive",conversation_id:"unselected",archive_navigation:"home",client_command_id:"background-home-archive"});
  expect(latestProjectionCommandIds.get("active-conversation")).toBe("chosen-command");
  expect(latestProjectionCommandIds.get("conversation-list")).toBe("background-home-archive");
});
