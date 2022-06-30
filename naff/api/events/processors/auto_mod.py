import logging
from typing import TYPE_CHECKING

from naff.client.const import logger_name
from naff.models.discord.auto_mod import AutoModerationAction, AutoModRule
from ._template import EventMixinTemplate, Processor
from ... import events

if TYPE_CHECKING:
    from naff.api.events import RawGatewayEvent

__all__ = ("AutoModEvents",)

log = logging.getLogger(logger_name)


class AutoModEvents(EventMixinTemplate):
    @Processor.define()
    async def _raw_auto_moderation_action_execution(self, event: "RawGatewayEvent") -> None:
        action = AutoModerationAction.from_dict(event.data.copy(), self)
        channel = self.get_channel(event.data["channel_id"])
        guild = self.get_guild(event.data["guild_id"])
        self.dispatch(events.AutoModExec(action, channel, guild))

    @Processor.define()
    async def raw_auto_moderation_rule_create(self, event: "RawGatewayEvent") -> None:
        rule = AutoModRule.from_dict(event.data, self)
        guild = self.get_guild(event.data["guild_id"])
        self.dispatch(events.AutoModCreated(guild, rule))

    @Processor.define()
    async def raw_auto_moderation_rule_delete(self, event: "RawGatewayEvent") -> None:
        rule = AutoModRule.from_dict(event.data, self)
        guild = self.get_guild(event.data["guild_id"])
        self.dispatch(events.AutoModUpdated(guild, rule))

    @Processor.define()
    async def raw_auto_moderation_rule_update(self, event: "RawGatewayEvent") -> None:
        rule = AutoModRule.from_dict(event.data, self)
        guild = self.get_guild(event.data["guild_id"])
        self.dispatch(events.AutoModDeleted(guild, rule))
