/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { MessagingMenu } from "@mail/core/public_web/messaging_menu";

// Patch the bell-dropdown inbox-message click handler so that clicking a
// notification opens the source record's form view (when one exists) instead
// of bouncing the user to the Discuss > Inbox list and forcing them to find
// the row again.
//
// Fallback path = standard Odoo behavior (open Discuss > Inbox). We fall back
// when:
//   - isMarkAsRead is true (user clicked the "mark as read" affordance, not the body)
//   - the message has no thread, or the thread has no model/id
//   - the thread is a discuss.channel (DMs / group chats — these should still
//     open Discuss, not try to load a fake form view)
//   - dispatching the act_window throws for any reason
patch(MessagingMenu.prototype, {
    onClickInboxMsg(isMarkAsRead, msg) {
        if (isMarkAsRead) {
            return super.onClickInboxMsg(isMarkAsRead, msg);
        }
        const thread = msg?.thread;
        const model = thread?.model;
        const resId = thread?.id;
        if (!model || !resId || model === "discuss.channel") {
            return super.onClickInboxMsg(isMarkAsRead, msg);
        }
        try {
            // Mark the underlying notification as read so the bell badge clears
            // — same UX as the standard handler's mark-as-done semantics, just
            // without dragging the user through the inbox list first.
            msg.setDone?.();
            this.env.services.action.doAction({
                type: "ir.actions.act_window",
                res_model: model,
                res_id: resId,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (err) {
            console.warn(
                "[mint_messaging_menu_jump] failed to open record, falling back to inbox:",
                err,
            );
            return super.onClickInboxMsg(isMarkAsRead, msg);
        }
    },
});
