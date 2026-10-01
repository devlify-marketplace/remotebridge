# deploy/ — Scripted Deployment (Phase 10)

Two independent paths to get the host onto a machine without someone
typing CLI flags by hand. Pick the one that matches the fleet; nothing
here is required to just run `python3 host_p12.py` yourself.

## Linux — `linux/install.sh`

Plain POSIX shell + a Python venv. **Actually run in the environment this
was built in** (directory layout, `deploy_config.json` placement, and the
generated systemd unit are all exercised for real) - the one thing that
isn't is the `pip install -r requirements.txt` step itself, since that
sandbox has no network access to PyPI; on a real machine with network
that step installs normally.

```bash
sudo ./linux/install.sh                              # system-wide, /opt
./linux/install.sh --user                            # just this user
./linux/install.sh --deploy-config ~/acme-deploy.json
```

Sets up a systemd unit (`--user` or system-wide, matching how you ran
it). Console-mode's interactive file-manager prompt (`console.py`) has
no TTY under systemd and exits immediately by design (see its own
`EOFError` handling) - the actual remote-desktop functionality doesn't
need a TTY at all and keeps running as a normal background service.

## Windows — `pyinstaller/` + `wix/`

PyInstaller freezes `host_p12.py` into `RemoteBridgeHost.exe`; WiX
Toolset packages that into an MSI. **Not run** in the environment this
was built in - no PyInstaller, no WiX, no Windows, and no network to
install any of the three. Both `host.spec` and `Product.wxs` are written
against their tools' documented CLIs/schemas and reviewed by hand;
treat them as reviewed, not proven, until they're actually built on a
real Windows machine with those tools installed.

```powershell
.\wix\build.ps1
.\wix\build.ps1 -DeployConfig C:\path\to\acme-deploy_config.json
```

Installs the exe + a Start Menu shortcut + clean uninstall support.
**Known limitation:** this installs an app you launch (from the Start
Menu, or a Scheduled Task you set up yourself), not a true Windows
service under the Service Control Manager - see `pyinstaller/host.spec`'s
comments. The console-mode caveat above applies here too, so "run via
Task Scheduler with no one logged in" already works today; a real SCM
service wrapper (for centralized start/stop/restart from `services.msc`)
is a reasonable follow-up, not something this pass builds.

## `deploy_config.template.json`

What either path drops next to the host so it enrolls with your admin
console with zero flags typed per machine. Copy it, fill in `admin_url`
and `enrollment_key` from the console's Deployment page, and pass the
result to either script above. Skip this entirely and both scripts still
work - the installed host just starts with no admin console configured,
same as Phase 9.

## What ties this to Phase 9

The admin console's own Deployment page (`/deployment`) shows the exact
`deploy_config.json` snippet for the org running it, and is also where
you publish a new release's version/URL/SHA-256 once one of these
builds is ready - see `../admin/README.md` and
`../docs/phases/PHASE_10_README.md`.
