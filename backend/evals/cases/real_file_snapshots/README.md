# Real task: file read snapshots and edit authorization

This case reverses the file-read and cache fix in commit `257a3713` onto a clean snapshot of commit `e658ffa7`, then removes the later regression test from the agent checkout. The agent sees only one baseline commit and a user report; `oracle.py` stays outside its workspace.

The external oracle checks that displayed content and the edit hash describe the same file snapshot, atomic replacement invalidates cached content even when size and mtime match, focused non-text reads respect the size limit, and a readable range without a complete UTF-8 snapshot grants no edit hash.
