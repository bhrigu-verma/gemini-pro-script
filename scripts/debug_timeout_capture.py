import time

import mainh as m


def run() -> None:
    d = m.attach_driver(m.DEBUG_PORT)
    handle = m.open_tab(d, m.GEMINI_URL, "DBG")
    time.sleep(4)
    tab = m.GeminiTab(d, handle, "DBG")
    tab.probe()

    prompt = "why do we got this error"
    tab.send(prompt)
    text = tab.recv()
    print("RECV_LEN", len(text))
    print("RECV_HEAD", repr((text or "")[:200]))

    tab.dump_dom("post_recv")
    ts = time.strftime("%Y%m%d_%H%M%S")
    tab.screenshot(str(m.OUTPUT_DIR / f"post_recv_dbg_{ts}.png"))
    d.quit()


if __name__ == "__main__":
    run()
