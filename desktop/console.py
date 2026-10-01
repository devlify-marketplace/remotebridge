"""
Phase 3-4 - interactive file-manager console.

Runs in its own thread on both host and viewer (the control channel is
symmetric). This is the two-pane file manager for this phase: `ls`
shows the peer's current directory, `lls` shows yours - side by side
in spirit, one command per pane, rather than one curses/GUI window.
See file_transfer.py's docstring for why a real drag-and-drop window
isn't part of this phase.

Phase 4 adds monitor switching and session recording, both viewer-only
(the viewer is the side with something to record and multiple displays
to choose between watching) - see monitors.py and recorder.py. Passing
`monitor_client`/`recorder`/`frame_size_ref` is optional; host.py's
console_loop call omits them and those commands print a clear "not
available here" instead of failing.

Commands:
  ls  [dir]       list the peer's directory (or the one you last cd'd to)
  cd <dir>        set which peer directory 'ls'/'get' operate on
  lls [dir]       list your own directory
  lcd <dir>       change your own directory (for 'lls'/'send')
  send <path>     push a local file to the peer
  get <path>      ask the peer to push that file to you
  monitors        list the host's monitors and which one is active
  monitor <n>     switch capture to monitor <n>
  record <path>   start recording the session to a video file (e.g. out.mp4)
  record stop     stop the current recording
  help            show this list
  quit            stop the file-manager console (the session keeps running)
"""

import os


def _print_listing(label: str, directory: str, entries: list) -> None:
    print(f"\n[{label}] {directory}")
    if not entries:
        print("  (empty or unreadable)")
        return
    for e in sorted(entries, key=lambda x: (not x["is_dir"], x["name"].lower())):
        tag = "<DIR>" if e["is_dir"] else f"{e['size']}B"
        print(f"  {e['name']:40s} {tag}")


def console_loop(session, monitor_client=None, recorder=None, frame_size_ref=None) -> None:
    print("\n[files] File manager ready. Type 'help' for commands.")
    remote_dir = "."
    while True:
        try:
            line = input("(files) > ").strip()
        except EOFError:
            break
        if not line:
            continue

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("quit", "exit"):
            print("[files] console stopped (session keeps running).")
            break

        elif cmd == "help":
            print(__doc__)

        elif cmd == "ls":
            reply = session.request_listing(arg or remote_dir)
            if reply is None:
                print("[files] peer did not respond to the listing request.")
            else:
                remote_dir = reply["dir"]
                _print_listing("peer", reply["dir"], reply["entries"])

        elif cmd == "cd":
            remote_dir = arg or "."
            print(f"[files] peer directory set to '{remote_dir}' for the next 'ls'/'get'.")

        elif cmd == "lls":
            target, entries = session.list_local(arg or None)
            _print_listing("you", target, entries)

        elif cmd == "lcd":
            try:
                os.chdir(arg or ".")
                session.cwd = os.getcwd()
                print(f"[files] local directory: {session.cwd}")
            except OSError as exc:
                print(f"[files] cannot cd: {exc}")

        elif cmd == "send":
            if not arg:
                print("[files] usage: send <local path>")
                continue
            session.send_file(arg)

        elif cmd == "get":
            if not arg:
                print("[files] usage: get <remote path>")
                continue
            remote_path = arg
            if not os.path.isabs(arg) and remote_dir not in (".", ""):
                remote_path = f"{remote_dir.rstrip('/')}/{arg}"
            session.request_file(remote_path)

        elif cmd == "monitors":
            if monitor_client is None:
                print("[monitors] not available here (host side lists its own monitors in its log, "
                      "not the console).")
                continue
            reply = monitor_client.request_list()
            if reply is None:
                print("[monitors] peer did not respond to the monitor list request.")
            else:
                print(f"\n[monitors] active: {reply['active']}")
                for m in reply["monitors"]:
                    tag = " (active)" if m["index"] == reply["active"] else ""
                    print(f"  #{m['index']}: {m['width']}x{m['height']} "
                          f"at ({m['left']},{m['top']}){tag}")

        elif cmd == "monitor":
            if monitor_client is None:
                print("[monitors] not available here.")
                continue
            if not arg or not arg.isdigit():
                print("[monitors] usage: monitor <n>  (see 'monitors' for valid numbers)")
                continue
            monitor_client.switch(int(arg))
            print(f"[monitors] requested switch to monitor {arg}.")

        elif cmd == "record":
            if recorder is None or frame_size_ref is None:
                print("[record] not available here (recording runs on the viewer side).")
                continue
            if arg == "stop":
                path = recorder.stop()
                print(f"[record] stopped, saved to {path}" if path else "[record] not recording.")
            elif not arg:
                print("[record] usage: record <path.mp4>   or   record stop")
            else:
                frame_size = frame_size_ref.get("win_size")
                if not frame_size or frame_size == (1, 1):
                    print("[record] no video frame received yet - try again in a moment.")
                    continue
                error = recorder.start(arg, frame_size)
                print(f"[record] {error}" if error else f"[record] recording to {arg}")

        else:
            print(f"[files] unknown command '{cmd}'. Type 'help'.")
