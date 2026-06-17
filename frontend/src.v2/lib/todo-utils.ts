import type { TodoItem } from "../stores/types";

let _counter = 0;

/**
 * Factory for creating a new TodoItem with a unique id and default values.
 */
export function createTodoItem(content: string, status: TodoItem["status"] = "pending"): TodoItem {
  _counter += 1;
  return {
    id: `todo-${Date.now()}-${_counter}`,
    content,
    activeForm: "",
    status,
  };
}
