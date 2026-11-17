import queue, db, util
auditor, analyst, executor = queue.Queue(), queue.Queue(), queue.Queue()
live = {"audit_running": None, "audit_layers": [], "executing": None}
def activity(title, sub, right="", kind="info", mint=None):
    db.q("insert into activity(ts,title,sub,right,kind) values(?,?,?,?,?)", (util.now_iso(), title, sub, right, kind))
    if kind == "reject" and mint:
        import config, notifier, links
        if config.load()["push_rejects"]: notifier.send(f"{title} · {sub}", [[{"text": "Open on FOMO", "url": links.coin_url(mint)}]])
