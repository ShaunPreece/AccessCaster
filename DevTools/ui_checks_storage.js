// probe: route=recording timeout=240
/* The card and drive section of the Recording page. Run:
     python DevTools/run_ui_probes.py storage

   Needs a server with /api/storage/*. What matters here: the unit's INTERNAL storage, which holds
   the pad sounds, is never offered; the eject button's label names what it will do; and erase never
   posts without the confirmation. POSTs are stubbed, so nothing reaches the desk. */

const el = (id) => document.getElementById(id);

async function run() {
  await waitFor(() => !/Reading the unit/i.test(el("rec-state").textContent), 25000);
  await sleep(600);
  const sel = el("rec-vol-select");
  check("the card or drive is a labelled native select",
    sel && sel.tagName === "SELECT" && !!document.querySelector("label[for='rec-vol-select']"));
  const opts = Array.from(sel.options).map((o) => o.textContent);
  check("the internal pad storage is never offered", !opts.some((t) => /internal/i.test(t)),
    opts.join(" | "));
  check("each option says its state", opts.every((t) => /: /.test(t)), opts.join(" | "));
  const eject = el("rec-vol-eject");
  check("eject is named for what it will do",
    /^(Eject|Reconnect)$/.test(eject.textContent.trim()), eject.textContent);
  check("neither button leaves the tab order", !eject.disabled && !el("rec-vol-erase").disabled);
  check("the page still has one live region",
    document.querySelectorAll("#sec-recording [aria-live]").length === 1);

  // Erase must ask first, and post nothing when the answer is no.
  const erase = el("rec-vol-erase");
  if (erase.getAttribute("aria-disabled") !== "true") {
    await probeClear();
    erase.click();
    const opened = await waitFor(() => {
      const d = document.querySelector("dialog[open]");
      return d && /erase/i.test(d.textContent);
    }, 3000);
    check("erase opens a confirmation first", opened);
    const dlg = document.querySelector("dialog[open]");
    const cancel = dlg && Array.from(dlg.querySelectorAll("button"))
      .find((b) => /cancel|no|keep/i.test(b.textContent));
    if (cancel) cancel.click();
    await sleep(600);
    const log = pagePosts(await probeLog());
    check("cancelling posts nothing", log.length === 0, JSON.stringify(log.map((r) => r.path)));
  }
  await probeDone();
}

probeRun(run);
