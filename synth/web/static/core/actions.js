// core/actions.js — actions that more than one place offers (the header and the Settings drawer).

/** Send every setting on the plate to the S-1 (POST /api/push-all): the explicit sync. Connecting
 *  only listens, so the S-1 keeps its own patch until this runs. Resolves true when it was sent. */
export async function sendPatch(ctx) {
  if (!ctx.server) {
    ctx.toast("This page has no cockpit behind it, so it cannot reach an S-1. Run the cockpit on your Mac.");
    return false;
  }
  if (ctx.status.demo) {
    ctx.toast("Demo mode: nothing was sent.");
    return false;
  }
  if (ctx.soundSource !== "s1") {
    ctx.toast("The S-1 is not connected. Plug it in with a USB cable, then send the patch.");
    return false;
  }
  try {
    const r = await ctx.server.api("POST", "/api/push-all");
    ctx.toast(`Sent ${r.pushed} settings to the S-1. The plate and the S-1 now match.`);
    return true;
  } catch (e) {
    ctx.toast(`The patch was not sent: ${e.message}`);
    return false;
  }
}
