def main(action: str = "list", meeting_id: str = "", title: str = "",
         audio_b64: str = "", started_at: str = "", new_root: str = "", duration: str = ""):
    import os, json, base64, shutil

    # Everything this app writes lives under one global per-app directory,
    # never in the app folder. Override with OPEN_MIC_CACHE_DIR.
    config_dir = os.environ.get("OPEN_MIC_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".fused-render", "cache", "open-mic")
    config_dir = os.path.abspath(os.path.expanduser(config_dir))
    config_path = os.path.join(config_dir, "config.json")
    default_root = os.path.join(config_dir, "meetings")

    # One-time migration from the pre-1.0 directory name ("openmic").
    if not os.path.exists(config_dir):
        legacy = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "openmic")
        if os.path.isdir(legacy):
            try:
                os.makedirs(os.path.dirname(config_dir), exist_ok=True)
                shutil.move(legacy, config_dir)
            except Exception:
                # Migration failed — keep reading the old location rather than
                # orphaning existing meetings.
                config_dir = legacy
                config_path = os.path.join(config_dir, "config.json")
                default_root = os.path.join(config_dir, "meetings")

    root = default_root
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        if cfg.get("root"):
            root = cfg["root"]
    except Exception:
        pass

    if action == "move_root":
        new_root = os.path.abspath(os.path.expanduser(new_root))
        if not new_root:
            raise ValueError("invalid path")
        if new_root == os.path.abspath(root):
            return {"ok": True, "root": root}
        os.makedirs(os.path.dirname(new_root) or "/", exist_ok=True)
        if os.path.isdir(root):
            if os.path.exists(new_root):
                for name in os.listdir(root):
                    dest = os.path.join(new_root, name)
                    if os.path.exists(dest):
                        shutil.rmtree(dest) if os.path.isdir(dest) else os.remove(dest)
                    shutil.move(os.path.join(root, name), dest)
                if not os.listdir(root):
                    os.rmdir(root)
            else:
                shutil.move(root, new_root)
        else:
            os.makedirs(new_root, exist_ok=True)
        os.makedirs(config_dir, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump({"root": new_root}, f, indent=2)
        return {"ok": True, "root": new_root}

    os.makedirs(root, exist_ok=True)

    def meta_path(mid):
        return os.path.join(root, mid, "meta.json")

    def merge_action_items(d):
        # Legacy layout kept action items in their own file; fold them into
        # summary.md (unless it already has that section) and drop the file.
        ap = os.path.join(d, "action-items.md")
        if not os.path.exists(ap):
            return
        try:
            with open(ap) as f:
                body = "\n".join(l for l in f.read().splitlines()
                                 if not l.startswith("# ")).strip()
            sp = os.path.join(d, "summary.md")
            summary = ""
            if os.path.exists(sp):
                with open(sp) as f:
                    summary = f.read()
            if body and "## Action items" not in summary:
                with open(sp, "a") as f:
                    f.write(("\n" if summary else "")
                            + "## Action items\n\n" + body + "\n")
            os.remove(ap)
        except Exception:
            pass

    if action == "list":
        out = []
        for mid in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, mid)
            if not os.path.isdir(d):
                continue
            merge_action_items(d)
            meta = {"id": mid, "title": mid}
            try:
                with open(meta_path(mid)) as f:
                    meta.update(json.load(f))
            except Exception:
                pass
            meta["files"] = sorted(
                n for n in os.listdir(d) if not n.startswith(".")
            )
            meta["has_audio"] = "audio.webm" in meta["files"]
            out.append(meta)
        return {"meetings": out, "root": root}

    if not meeting_id or "/" in meeting_id or ".." in meeting_id:
        raise ValueError("invalid meeting_id")
    d = os.path.join(root, meeting_id)

    if action == "create":
        os.makedirs(d, exist_ok=True)
        meta = {"id": meeting_id, "title": title or meeting_id,
                "started_at": started_at}
        with open(meta_path(meeting_id), "w") as f:
            json.dump(meta, f, indent=2)
        # Empty notes file ready to be filled in during/after the meeting.
        p = os.path.join(d, "notes.md")
        if not os.path.exists(p):
            with open(p, "w") as f:
                f.write("# Notes — " + (title or meeting_id) + "\n\n")
        return {"ok": True, "dir": d}

    if action == "set_title":
        meta = {"id": meeting_id}
        try:
            with open(meta_path(meeting_id)) as f:
                meta.update(json.load(f))
        except Exception:
            pass
        if title:
            meta["title"] = title
        if duration:
            meta["duration"] = duration
        with open(meta_path(meeting_id), "w") as f:
            json.dump(meta, f, indent=2)
        return {"ok": True, "title": meta.get("title"), "duration": meta.get("duration")}

    if action == "delete":
        import shutil
        if os.path.isdir(d):
            shutil.rmtree(d)
        return {"ok": True}

    if action == "append_audio":
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "audio.webm"), "ab") as f:
            f.write(base64.b64decode(audio_b64))
        return {"ok": True}

    raise ValueError("unknown action: " + action)
