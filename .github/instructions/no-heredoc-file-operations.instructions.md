---
name: 'No Heredoc File Operations'
description: 'Use file editing tools, not shell redirection, to write file content or multi-line text'
applyTo: '**'
---

# No shell-written file content

Multi-line text passed inline through the terminal gets corrupted in this environment (tabs trigger completion, quotes and backticks are mangled, output is truncated or duplicated).

Never use the terminal to write literal content:

- Heredocs (`<<`, `<<'EOF'`, `<<-EOF`)
- `cat`, `echo`, `printf`, `tee`, `>` or `>>` with multi-line content
- Multi-line `git commit -m "..."`

Instead:

- Create or edit files with the file creation and replace-in-file tools.
- For multi-line commit messages, write the message to a file with the create tool, then run `git commit -F <file>`. Verify with `git log -1 --format=%B`.
- For scratch scripts, create the file with the create tool, then run it.

Allowed in the terminal: `rm`, `mv`, `cp`, `mkdir`, and tools that edit files themselves (`uv add`, `sed -i`, `pytest --update-golden`).
