# Release Notes - 7.4.2

A hotfix for the BridgeSync KOReader plugin. If you installed the plugin from
7.4.0 or 7.4.1, this is the one you want.

Every network operation in plugin versions **0.6.1 through 0.6.4** ran itself
twice over — the plugin started a background process to do the work, and then
that process started another one to make the actual request. On Kindle that left
two copies of KOReader running against the same screen and the same input
devices: tapping **Test Connection** crashed the device and restarted it. On
Android the inner process died before it could report back, so the plugin
answered **"Authentication failed"** or **"Version check failed"** no matter how
correct your server URL and credentials were. Book sync, reading-stats sync and
highlight sync all travelled the same path.

The plugin is now **0.6.5**. Nothing on the server side changed in this release.

## Fixed

- **Test Connection no longer crashes Kindles, and authentication works again.**
  A background process is no longer allowed to start another one; when it is
  already running as one, it does the work directly instead. (#370, #401)
- **A background operation that crashes no longer reports itself as a rejected
  login.** When one exited without returning a result, the plugin read that as
  success-with-nothing-in-it and fell back to its generic wording, so a hard
  crash reached you as a wrong username and password. It now reports the
  operation as failed, and says so.

## Upgrading

**You have to re-download the plugin by hand. It cannot update itself out of
this** — "Check for Plugin Update" is one of the operations the bug breaks, so
no device running 0.6.1-0.6.4 can pull the fix through the plugin.

On each device:

1. Update BookBridge to 7.4.2 and let it restart.
2. Open your BookBridge **account page** and use **Download plugin (.zip)**, or
   take `bridgesync-0.6.5.zip` from the GitHub release.
3. Unzip it into `koreader/plugins/`, replacing the existing
   `bridgesync.koplugin` folder.
4. Restart KOReader, and confirm the account page shows **v0.6.5**.

## Operational Notes

- No database migration and no settings changes.
- The server rebuilds the downloadable plugin zip automatically when the plugin
  files change, so no extra step is needed on the server.
- If you were pointing BridgeSync at something other than BookBridge's KoSync
  address, that is a separate misconfiguration — the plugin's server URL must be
  your BookBridge KoSync URL (port 5758 by default), not another reader's.
