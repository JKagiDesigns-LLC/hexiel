# hexiel for VS Code

A chat panel that runs your local `hexiel` (`hexiel --acp`, the Agent Client Protocol) — your models, your keys, your machine.

1. `pipx install hexiel` (or `pip install hexiel`), then check `hexiel --version` works in a terminal.
2. Install this extension: `cd editors/vscode && npx @vscode/vsce package` then *Extensions → Install from VSIX…*
   (or open this folder in VS Code and press F5 to try it).
3. **hexiel: Open chat** from the command palette. Select code and press `Ctrl+Alt+H` (`Cmd+Alt+H`) to ask about it.

File edits ask permission in the panel; every edit has a **View diff** button. Settings: `hexiel.command`, `hexiel.args`
(add `-m PROFILE` to pin a model). Tests: `npm test` (runs the protocol client against a fake agent, no model needed).

## Publishing (maintainers)

Publisher: **JKagiDesignsLLC** (marketplace.visualstudio.com/manage/publishers/jkagidesignsllc). Create a Personal Access Token
at dev.azure.com (Organization: *All accessible organizations*, scope *Marketplace → Manage*), then:

```
cd editors/vscode
npx @vscode/vsce login JKagiDesignsLLC      # paste the token
npx @vscode/vsce publish                    # or: vsce package, then upload the .vsix on the publisher page
```
