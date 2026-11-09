// Demo entry point: install the in-browser backend, then boot the real app.

import { installDemoFetch } from "./backend/http";
import { seedDemoConversations } from "./backend/seed";
import { installDemoSocket } from "./backend/socket";

seedDemoConversations();
installDemoFetch();
installDemoSocket();

void import("../main");
