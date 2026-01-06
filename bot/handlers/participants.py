from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from bot.helpers import send_participants_editor, send_subscription_detail
from bot.states import ParticipantAction, SubscriptionAction
from database import Database

from . import admin_router

MAX_SHARE_WEIGHT = 5


@admin_router.callback_query(SubscriptionAction.filter(F.action == "participants"))
async def handle_subscription_participants(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    await send_participants_editor(callback, db, callback_data.subscription_id)


@admin_router.callback_query(ParticipantAction.filter(F.action == "toggle"))
async def handle_participant_toggle(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> None:
    friends = await db.list_friends_with_membership(callback_data.subscription_id)
    target = next((f for f in friends if f["id"] == callback_data.friend_id), None)
    if target is None:
        await callback.answer("Unknown member.", show_alert=True)
        return

    await db.set_participant(
        subscription_id=callback_data.subscription_id,
        friend_id=callback_data.friend_id,
        enabled=not bool(target["is_member"]),
    )
    await send_participants_editor(callback, db, callback_data.subscription_id)


@admin_router.callback_query(ParticipantAction.filter(F.action == "weight"))
async def handle_participant_weight(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> None:
    friends = await db.list_friends_with_membership(callback_data.subscription_id)
    target = next((f for f in friends if f["id"] == callback_data.friend_id), None)
    if target is None:
        await callback.answer("Unknown member.", show_alert=True)
        return
    if not target["is_member"]:
        await callback.answer("Add the member first.", show_alert=True)
        return

    current_weight = int(target.get("share_weight") or 1)
    new_weight = 1 if current_weight >= MAX_SHARE_WEIGHT else current_weight + 1
    await db.update_participant_weight(
        subscription_id=callback_data.subscription_id,
        friend_id=callback_data.friend_id,
        share_weight=new_weight,
    )
    await send_participants_editor(callback, db, callback_data.subscription_id)


@admin_router.callback_query(ParticipantAction.filter(F.action == "done"))
async def handle_participant_done(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> None:
    await send_subscription_detail(callback, db, callback_data.subscription_id)
