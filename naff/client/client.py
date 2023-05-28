import asyncio
import importlib.util
import inspect
import json
import logging
import re
import sys
import time
import traceback
from collections.abc import Iterable
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    NoReturn,
    Optional,
    Sequence,
    Type,
    Union,
    overload,
    Literal,
)

from discord_typings._interactions._receiving import (
    ComponentChannelInteractionData,
    AutocompleteChannelInteractionData,
    InteractionData,
)

import naff.api.events as events
import naff.client.const as constants
from naff.api.events import BaseEvent, Component, RawGatewayEvent, processors, MessageCreate
from naff.api.gateway.gateway import GatewayClient
from naff.api.gateway.state import ConnectionState
from naff.api.http.http_client import HTTPClient
from naff.client import errors
from naff.client.const import GLOBAL_SCOPE, MISSING, MENTION_PREFIX, Absent, EMBED_MAX_DESC_LENGTH, get_logger
from naff.client.errors import (
    BotException,
    ExtensionLoadException,
    ExtensionNotFound,
    Forbidden,
    InteractionMissingAccess,
    HTTPException,
    NotFound,
)
from naff.client.smart_cache import GlobalCache
from naff.client.utils import NullCache
from naff.client.utils.input_utils import get_first_word, get_args
from naff.client.utils.misc_utils import get_event_name, wrap_partial
from naff.client.utils.serializer import to_image_data
from naff.models import (
    Activity,
    Application,
    CustomEmoji,
    Guild,
    GuildTemplate,
    Message,
    Extension,
    NaffUser,
    User,
    Member,
    StickerPack,
    Sticker,
    ScheduledEvent,
    InteractionCommand,
    SlashCommand,
    OptionTypes,
    HybridCommand,
    PrefixedCommand,
    BaseCommand,
    to_snowflake,
    to_snowflake_list,
    ComponentContext,
    InteractionContext,
    ModalContext,
    PrefixedContext,
    AutocompleteContext,
    HybridContext,
    ComponentCommand,
    application_commands_to_dict,
    sync_needed,
    VoiceRegion,
)
from naff.models import Wait
from naff.models.discord.channel import BaseChannel
from naff.models.discord.color import BrandColors
from naff.models.discord.components import get_components_ids, BaseComponent
from naff.models.discord.embed import Embed
from naff.models.discord.enums import ComponentTypes, Intents, InteractionTypes, Status, ChannelTypes
from naff.models.discord.file import UPLOADABLE_TYPE
from naff.models.discord.modal import Modal
from naff.models.naff.active_voice_state import ActiveVoiceState
from naff.models.naff.application_commands import ContextMenu, ModalCommand
from naff.models.naff.auto_defer import AutoDefer
from naff.models.naff.hybrid_commands import _prefixed_from_slash, _base_subcommand_generator
from naff.models.naff.listener import Listener
from naff.models.naff.tasks import Task

if TYPE_CHECKING:
    from naff.models import Snowflake_Type, TYPE_ALL_CHANNEL


__all__ = ("Client",)


# see https://discord.com/developers/docs/topics/gateway#list-of-intents
_INTENT_EVENTS: dict[BaseEvent, list[Intents]] = {
    # Intents.GUILDS
    events.GuildJoin: [Intents.GUILDS],
    events.GuildLeft: [Intents.GUILDS],
    events.GuildUpdate: [Intents.GUILDS],
    events.RoleCreate: [Intents.GUILDS],
    events.RoleDelete: [Intents.GUILDS],
    events.RoleUpdate: [Intents.GUILDS],
    events.ChannelCreate: [Intents.GUILDS],
    events.ChannelDelete: [Intents.GUILDS],
    events.ChannelUpdate: [Intents.GUILDS],
    events.ThreadCreate: [Intents.GUILDS],
    events.ThreadDelete: [Intents.GUILDS],
    events.ThreadListSync: [Intents.GUILDS],
    events.ThreadMemberUpdate: [Intents.GUILDS],
    events.ThreadUpdate: [Intents.GUILDS],
    events.StageInstanceCreate: [Intents.GUILDS],
    events.StageInstanceDelete: [Intents.GUILDS],
    events.StageInstanceUpdate: [Intents.GUILDS],
    # Intents.GUILD_MEMBERS
    events.MemberAdd: [Intents.GUILD_MEMBERS],
    events.MemberRemove: [Intents.GUILD_MEMBERS],
    events.MemberUpdate: [Intents.GUILD_MEMBERS],
    # Intents.GUILD_BANS
    events.BanCreate: [Intents.GUILD_BANS],
    events.BanRemove: [Intents.GUILD_BANS],
    # Intents.GUILD_EMOJIS_AND_STICKERS
    events.GuildEmojisUpdate: [Intents.GUILD_EMOJIS_AND_STICKERS],
    events.GuildStickersUpdate: [Intents.GUILD_EMOJIS_AND_STICKERS],
    # Intents.GUILD_BANS
    events.IntegrationCreate: [Intents.GUILD_INTEGRATIONS],
    events.IntegrationDelete: [Intents.GUILD_INTEGRATIONS],
    events.IntegrationUpdate: [Intents.GUILD_INTEGRATIONS],
    # Intents.GUILD_WEBHOOKS
    events.WebhooksUpdate: [Intents.GUILD_WEBHOOKS],
    # Intents.GUILD_INVITES
    events.InviteCreate: [Intents.GUILD_INVITES],
    events.InviteDelete: [Intents.GUILD_INVITES],
    # Intents.GUILD_VOICE_STATES
    events.VoiceStateUpdate: [Intents.GUILD_VOICE_STATES],
    # Intents.GUILD_PRESENCES
    events.PresenceUpdate: [Intents.GUILD_PRESENCES],
    # Intents.GUILD_MESSAGES
    events.MessageDeleteBulk: [Intents.GUILD_MESSAGES],
    # Intents.AUTO_MODERATION_CONFIGURATION
    events.AutoModExec: [Intents.AUTO_MODERATION_EXECUTION, Intents.AUTO_MOD],
    # Intents.AUTO_MODERATION_CONFIGURATION
    events.AutoModCreated: [Intents.AUTO_MODERATION_CONFIGURATION, Intents.AUTO_MOD],
    events.AutoModUpdated: [Intents.AUTO_MODERATION_CONFIGURATION, Intents.AUTO_MOD],
    events.AutoModDeleted: [Intents.AUTO_MODERATION_CONFIGURATION, Intents.AUTO_MOD],
    # multiple intents
    events.ThreadMembersUpdate: [Intents.GUILDS, Intents.GUILD_MEMBERS],
    events.TypingStart: [Intents.GUILD_MESSAGE_TYPING, Intents.DIRECT_MESSAGE_TYPING, Intents.TYPING],
    events.MessageUpdate: [Intents.GUILD_MESSAGES, Intents.DIRECT_MESSAGES, Intents.MESSAGES],
    events.MessageCreate: [Intents.GUILD_MESSAGES, Intents.DIRECT_MESSAGES, Intents.MESSAGES],
    events.MessageDelete: [Intents.GUILD_MESSAGES, Intents.DIRECT_MESSAGES, Intents.MESSAGES],
    events.ChannelPinsUpdate: [Intents.GUILDS, Intents.DIRECT_MESSAGES],
    events.MessageReactionAdd: [Intents.GUILD_MESSAGE_REACTIONS, Intents.DIRECT_MESSAGE_REACTIONS, Intents.REACTIONS],
    events.MessageReactionRemove: [
        Intents.GUILD_MESSAGE_REACTIONS,
        Intents.DIRECT_MESSAGE_REACTIONS,
        Intents.REACTIONS,
    ],
    events.MessageReactionRemoveAll: [
        Intents.GUILD_MESSAGE_REACTIONS,
        Intents.DIRECT_MESSAGE_REACTIONS,
        Intents.REACTIONS,
    ],
}


class Client(
    processors.AutoModEvents,
    processors.ChannelEvents,
    processors.GuildEvents,
    processors.IntegrationEvents,
    processors.MemberEvents,
    processors.MessageEvents,
    processors.ReactionEvents,
    processors.RoleEvents,
    processors.StageEvents,
    processors.ThreadEvents,
    processors.UserEvents,
    processors.VoiceEvents,
):
    """

    The bot client.

    Args:
        intents: The intents to use

        default_prefix: The default prefix (or prefixes) to use for prefixed commands. Defaults to your bot being mentioned.
        generate_prefixes: A coroutine that returns a string or an iterable of strings to determine prefixes.
        status: The status the bot should log in with (IE ONLINE, DND, IDLE)
        activity: The activity the bot should log in "playing"

        sync_interactions: Should application commands be synced with discord?
        delete_unused_application_cmds: Delete any commands from discord that aren't implemented in this client
        enforce_interaction_perms: Enforce discord application command permissions, locally
        fetch_members: Should the client fetch members from guilds upon startup (this will delay the client being ready)
        send_command_tracebacks: Automatically send uncaught tracebacks if a command throws an exception

        auto_defer: AutoDefer: A system to automatically defer commands after a set duration
        interaction_context: Type[InteractionContext]: InteractionContext: The object to instantiate for Interaction Context
        prefixed_context: Type[PrefixedContext]: The object to instantiate for Prefixed Context
        component_context: Type[ComponentContext]: The object to instantiate for Component Context
        autocomplete_context: Type[AutocompleteContext]: The object to instantiate for Autocomplete Context
        modal_context: Type[ModalContext]: The object to instantiate for Modal Context
        hybrid_context: Type[HybridContext]: The object to instantiate for Hybrid Context

        total_shards: The total number of shards in use
        shard_id: The zero based int ID of this shard

        debug_scope: Force all application commands to be registered within this scope
        disable_dm_commands: Should interaction commands be disabled in DMs?
        basic_logging: Utilise basic logging to output library data to console. Do not use in combination with `Client.logger`
        logging_level: The level of logging to use for basic_logging. Do not use in combination with `Client.logger`
        logger: The logger NAFF should use. Do not use in combination with `Client.basic_logging` and `Client.logging_level`. Note: Different loggers with multiple clients are not supported

    Optionally, you can configure the caches here, by specifying the name of the cache, followed by a dict-style object to use.
    It is recommended to use `smart_cache.create_cache` to configure the cache here.
    as an example, this is a recommended attribute `message_cache=create_cache(250, 50)`,

    ???+ note "Intents Note"
        By default, all non-privileged intents will be enabled

    ???+ note "Caching Note"
        Setting a message cache hard limit to None is not recommended, as it could result in extremely high memory usage, we suggest a sane limit.


    """

    def __init__(
        self,
        *,
        activity: Union[Activity, str] = None,
        auto_defer: Absent[Union[AutoDefer, bool]] = MISSING,
        autocomplete_context: Type[AutocompleteContext] = AutocompleteContext,
        component_context: Type[ComponentContext] = ComponentContext,
        debug_scope: Absent["Snowflake_Type"] = MISSING,
        default_prefix: str | Iterable[str] = MENTION_PREFIX,
        delete_unused_application_cmds: bool = False,
        disable_dm_commands: bool = False,
        enforce_interaction_perms: bool = True,
        fetch_members: bool = False,
        generate_prefixes: Absent[Callable[..., Coroutine]] = MISSING,
        global_post_run_callback: Absent[Callable[..., Coroutine]] = MISSING,
        global_pre_run_callback: Absent[Callable[..., Coroutine]] = MISSING,
        intents: Union[int, Intents] = Intents.DEFAULT,
        interaction_context: Type[InteractionContext] = InteractionContext,
        logger: logging.Logger = MISSING,
        owner_ids: Iterable["Snowflake_Type"] = (),
        modal_context: Type[ModalContext] = ModalContext,
        prefixed_context: Type[PrefixedContext] = PrefixedContext,
        hybrid_context: Type[HybridContext] = HybridContext,
        send_command_tracebacks: bool = True,
        shard_id: int = 0,
        status: Status = Status.ONLINE,
        sync_interactions: bool = True,
        sync_ext: bool = True,
        total_shards: int = 1,
        basic_logging: bool = False,
        logging_level: int = logging.INFO,
        **kwargs,
    ) -> None:
        if logger is MISSING:
            logger = constants.get_logger()

        if basic_logging:
            logging.basicConfig()
            logger.setLevel(logging_level)

        # Set Up logger and overwrite the constant
        self.logger = logger
        """The logger NAFF should use. Do not use in combination with `Client.basic_logging` and `Client.logging_level`.
        !!! note
            Different loggers with multiple clients are not supported"""
        constants._logger = logger

        # Configuration
        self.sync_interactions: bool = sync_interactions
        """Should application commands be synced"""
        self.del_unused_app_cmd: bool = delete_unused_application_cmds
        """Should unused application commands be deleted?"""
        self.sync_ext: bool = sync_ext
        """Should we sync whenever a extension is (un)loaded"""
        self.debug_scope = to_snowflake(debug_scope) if debug_scope is not MISSING else MISSING
        """Sync global commands as guild for quicker command updates during debug"""
        self.default_prefix = default_prefix
        """The default prefix to be used for prefixed commands"""
        self.generate_prefixes = generate_prefixes if generate_prefixes is not MISSING else self.generate_prefixes
        """A coroutine that returns a prefix or an iterable of prefixes, for dynamic prefixes"""
        self.send_command_tracebacks: bool = send_command_tracebacks
        """Should the traceback of command errors be sent in reply to the command invocation"""
        if auto_defer is True:
            auto_defer = AutoDefer(enabled=True)
        else:
            auto_defer = auto_defer or AutoDefer()
        self.auto_defer = auto_defer
        """A system to automatically defer commands after a set duration"""
        self.intents = intents if isinstance(intents, Intents) else Intents(intents)

        # resources

        self.http: HTTPClient = HTTPClient(logger=self.logger)
        """The HTTP client to use when interacting with discord endpoints"""

        # context objects
        self.interaction_context: Type[InteractionContext] = interaction_context
        """The object to instantiate for Interaction Context"""
        self.prefixed_context: Type[PrefixedContext] = prefixed_context
        """The object to instantiate for Prefixed Context"""
        self.component_context: Type[ComponentContext] = component_context
        """The object to instantiate for Component Context"""
        self.autocomplete_context: Type[AutocompleteContext] = autocomplete_context
        """The object to instantiate for Autocomplete Context"""
        self.modal_context: Type[ModalContext] = modal_context
        """The object to instantiate for Modal Context"""
        self.hybrid_context: Type[HybridContext] = hybrid_context
        """The object to instantiate for Hybrid Context"""

        # flags
        self._ready = asyncio.Event()
        self._closed = False
        self._startup = False
        self.disable_dm_commands = disable_dm_commands

        self._guild_event = asyncio.Event()
        self.guild_event_timeout = 3
        """How long to wait for guilds to be cached"""

        # Sharding
        self.total_shards = total_shards
        self._connection_state: ConnectionState = ConnectionState(self, intents, shard_id)

        self.enforce_interaction_perms = enforce_interaction_perms

        self.fetch_members = fetch_members
        """Fetch the full members list of all guilds on startup"""

        self._mention_reg = MISSING

        # caches
        self.cache: GlobalCache = GlobalCache(self, **{k: v for k, v in kwargs.items() if hasattr(GlobalCache, k)})
        # these store the last sent presence data for change_presence
        self._status: Status = status
        if isinstance(activity, str):
            self._activity = Activity.create(name=str(activity))
        else:
            self._activity: Activity = activity

        self._user: Absent[NaffUser] = MISSING
        self._app: Absent[Application] = MISSING

        # collections
        self.prefixed_commands: Dict[str, PrefixedCommand] = {}
        """A dictionary of registered prefixed commands: `{name: command}`"""
        self.interactions: Dict["Snowflake_Type", Dict[str, InteractionCommand]] = {}
        """A dictionary of registered application commands: `{cmd_id: command}`"""
        self.interaction_tree: Dict[
            "Snowflake_Type", Dict[str, InteractionCommand | Dict[str, InteractionCommand]]
        ] = {}
        """A dictionary of registered application commands in a tree"""
        self._component_callbacks: Dict[str, Callable[..., Coroutine]] = {}
        self._modal_callbacks: Dict[str, Callable[..., Coroutine]] = {}
        self._interaction_scopes: Dict["Snowflake_Type", "Snowflake_Type"] = {}
        self.processors: Dict[str, Callable[..., Coroutine]] = {}
        self.__modules = {}
        self.ext = {}
        """A dictionary of mounted ext"""
        self.listeners: Dict[str, list[Listener]] = {}
        self.waits: Dict[str, List] = {}
        self.owner_ids: set[Snowflake_Type] = set(owner_ids)

        self.async_startup_tasks: list[Coroutine] = []
        """A list of coroutines to run during startup"""

        # callbacks
        if global_pre_run_callback:
            if asyncio.iscoroutinefunction(global_pre_run_callback):
                self.pre_run_callback: Callable[..., Coroutine] = global_pre_run_callback
            else:
                raise TypeError("Callback must be a coroutine")
        else:
            self.pre_run_callback = MISSING

        if global_post_run_callback:
            if asyncio.iscoroutinefunction(global_post_run_callback):
                self.post_run_callback: Callable[..., Coroutine] = global_post_run_callback
            else:
                raise TypeError("Callback must be a coroutine")
        else:
            self.post_run_callback = MISSING

        super().__init__()
        self._sanity_check()

    @property
    def is_closed(self) -> bool:
        """Returns True if the bot has closed."""
        return self._closed

    @property
    def is_ready(self) -> bool:
        """Returns True if the bot is ready."""
        return self._ready.is_set()

    @property
    def latency(self) -> float:
        """Returns the latency of the websocket connection."""
        return self._connection_state.latency

    @property
    def average_latency(self) -> float:
        """Returns the average latency of the websocket connection."""
        return self._connection_state.average_latency

    @property
    def start_time(self) -> datetime:
        """The start time of the bot."""
        return self._connection_state.start_time

    @property
    def gateway_started(self) -> bool:
        """Returns if the gateway has been started."""
        return self._connection_state.gateway_started.is_set()

    @property
    def user(self) -> NaffUser:
        """Returns the bot's user."""
        return self._user

    @property
    def app(self) -> Application:
        """Returns the bots application."""
        return self._app

    @property
    def owner(self) -> Optional["User"]:
        """Returns the bot's owner'."""
        try:
            return self.app.owner
        except TypeError:
            return MISSING

    @property
    def owners(self) -> List["User"]:
        """Returns the bot's owners as declared via `client.owner_ids`."""
        return [self.get_user(u_id) for u_id in self.owner_ids]

    @property
    def guilds(self) -> List["Guild"]:
        """Returns a list of all guilds the bot is in."""
        return self.user.guilds

    @property
    def status(self) -> Status:
        """
        Get the status of the bot.

        IE online, afk, dnd

        """
        return self._status

    @property
    def activity(self) -> Activity:
        """Get the activity of the bot."""
        return self._activity

    @property
    def application_commands(self) -> List[InteractionCommand]:
        """A list of all application commands registered within the bot."""
        commands = []
        for scope in self.interactions.keys():
            commands += [cmd for cmd in self.interactions[scope].values() if cmd not in commands]

        return commands

    @property
    def ws(self) -> GatewayClient:
        """Returns the websocket client."""
        return self._connection_state.gateway

    def get_guild_websocket(self, id: "Snowflake_Type") -> GatewayClient:
        return self.ws

    def _sanity_check(self) -> None:
        """Checks for possible and common errors in the bot's configuration."""
        self.logger.debug("Running client sanity checks...")
        contexts = {
            self.interaction_context: InteractionContext,
            self.prefixed_context: PrefixedContext,
            self.component_context: ComponentContext,
            self.autocomplete_context: AutocompleteContext,
            self.modal_context: ModalContext,
            self.hybrid_context: HybridContext,
        }
        for obj, expected in contexts.items():
            if not issubclass(obj, expected):
                raise TypeError(f"{obj.__name__} must inherit from {expected.__name__}")

        if self.del_unused_app_cmd:
            self.logger.warning(
                "As `delete_unused_application_cmds` is enabled, the client must cache all guilds app-commands, this could take a while."
            )

        if Intents.GUILDS not in self._connection_state.intents:
            self.logger.warning("GUILD intent has not been enabled; this is very likely to cause errors")

        if self.fetch_members and Intents.GUILD_MEMBERS not in self._connection_state.intents:
            raise BotException("Members Intent must be enabled in order to use fetch members")
        elif self.fetch_members:
            self.logger.warning("fetch_members enabled; startup will be delayed")

        if len(self.processors) == 0:
            self.logger.warning("No Processors are loaded! This means no events will be processed!")

        caches = [
            c[0]
            for c in inspect.getmembers(self.cache, predicate=lambda x: isinstance(x, dict))
            if not c[0].startswith("__")
        ]
        for cache in caches:
            _cache_obj = getattr(self.cache, cache)
            if isinstance(_cache_obj, NullCache):
                self.logger.warning(f"{cache} has been disabled")

    async def generate_prefixes(self, bot: "Client", message: Message) -> str | Iterable[str]:
        """
        A method to get the bot's default_prefix, can be overridden to add dynamic prefixes.

        !!! note
            To easily override this method, simply use the `generate_prefixes` parameter when instantiating the client

        Args:
            bot: A reference to the client
            message: A message to determine the prefix from.

        Example:
            ```python
            async def generate_prefixes(bot, message):
                if message.guild.id == 870046872864165888:
                    return ["!"]
                return bot.default_prefix

            bot = Client(generate_prefixes=generate_prefixes, ...)
            ```

        Returns:
            A string or an iterable of strings to use as a prefix. By default, this will return `client.default_prefix`

        """
        return self.default_prefix

    def _queue_task(self, coro: Listener, event: BaseEvent, *args, **kwargs) -> asyncio.Task:
        async def _async_wrap(_coro: Listener, _event: BaseEvent, *_args, **_kwargs) -> None:
            try:
                if not isinstance(_event, (events.Error, events.RawGatewayEvent)):
                    if coro.delay_until_ready and not self.is_ready:
                        await self.wait_until_ready()

                if len(_event.__attrs_attrs__) == 2:
                    # override_name & bot
                    await _coro()
                else:
                    await _coro(_event, *_args, **_kwargs)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if isinstance(event, events.Error):
                    # No infinite loops please
                    self.default_error_handler(repr(event), e)
                else:
                    self.dispatch(events.Error(source=repr(event), error=e))

        wrapped = _async_wrap(coro, event, *args, **kwargs)

        return asyncio.create_task(wrapped, name=f"naff:: {event.resolved_name}")

    @staticmethod
    def default_error_handler(source: str, error: BaseException) -> None:
        """
        The default error logging behaviour.

        Args:
            source: The source of this error
            error: The exception itself

        """
        out = traceback.format_exception(error)

        if isinstance(error, HTTPException):
            # HTTPException's are of 3 known formats, we can parse them for human readable errors
            try:
                out = [str(error)]
            except Exception:  # noqa : S110
                pass

        get_logger().error(
            "Ignoring exception in {}:{}{}".format(source, "\n" if len(out) > 1 else " ", "".join(out)),
        )

    @Listener.create(is_default_listener=True)
    async def on_error(self, event: events.Error) -> None:
        """
        Catches all errors dispatched by the library.

        By default it will format and print them to console.

        Listen to the `Error` event to overwrite this behaviour.

        """
        self.default_error_handler(event.source, event.error)

    @Listener.create(is_default_listener=True)
    async def on_command_error(self, event: events.CommandError) -> None:
        """
        Catches all errors dispatched by commands.

        By default it will dispatch the `Error` event.

        Listen to the `CommandError` event to overwrite this behaviour.

        """
        self.dispatch(
            events.Error(
                source=f"cmd `/{event.ctx.invoke_target}`",
                error=event.error,
                args=event.args,
                kwargs=event.kwargs,
                ctx=event.ctx,
            )
        )
        try:
            if isinstance(event.error, errors.CommandOnCooldown):
                await event.ctx.send(
                    embeds=Embed(
                        description=f"This command is on cooldown!\n"
                        f"Please try again in {int(event.error.cooldown.get_cooldown_time())} seconds",
                        color=BrandColors.FUCHSIA,
                    )
                )
            elif isinstance(event.error, errors.MaxConcurrencyReached):
                await event.ctx.send(
                    embeds=Embed(
                        description="This command has reached its maximum concurrent usage!\n"
                        "Please try again shortly.",
                        color=BrandColors.FUCHSIA,
                    )
                )
            elif isinstance(event.error, errors.CommandCheckFailure):
                await event.ctx.send(
                    embeds=Embed(
                        description="You do not have permission to run this command!",
                        color=BrandColors.YELLOW,
                    )
                )
            elif self.send_command_tracebacks:
                out = "".join(traceback.format_exception(event.error))
                if self.http.token is not None:
                    out = out.replace(self.http.token, "[REDACTED TOKEN]")
                await event.ctx.send(
                    embeds=Embed(
                        title=f"Error: {type(event.error).__name__}",
                        color=BrandColors.RED,
                        description=f"```\n{out[:EMBED_MAX_DESC_LENGTH-8]}```",
                    )
                )
        except errors.NaffException:
            pass

    @Listener.create(is_default_listener=True)
    async def on_command_completion(self, event: events.CommandCompletion) -> None:
        """
        Called *after* any command is ran.

        By default, it will simply log the command.

        Listen to the `CommandCompletion` event to overwrite this behaviour.

        """
        if isinstance(event.ctx, PrefixedContext):
            symbol = "@"
        elif isinstance(event.ctx, InteractionContext):
            symbol = "/"
        elif isinstance(event.ctx, HybridContext):
            symbol = "@/"
        else:
            symbol = "?"  # likely custom context
        self.logger.info(
            f"Command Called: {symbol}{event.ctx.invoke_target} with {event.ctx.args = } | {event.ctx.kwargs = }"
        )

    @Listener.create(is_default_listener=True)
    async def on_component_error(self, event: events.ComponentError) -> None:
        """
        Catches all errors dispatched by components.

        By default it will dispatch the `Error` event.

        Listen to the `ComponentError` event to overwrite this behaviour.

        """
        self.dispatch(
            events.Error(
                source=f"Component Callback for {event.ctx.custom_id}",
                error=event.error,
                args=event.args,
                kwargs=event.kwargs,
                ctx=event.ctx,
            )
        )

    @Listener.create(is_default_listener=True)
    async def on_component_completion(self, event: events.ComponentCompletion) -> None:
        """
        Called *after* any component callback is ran.

        By default, it will simply log the component use.

        Listen to the `ComponentCompletion` event to overwrite this behaviour.

        """
        symbol = "¢"
        self.logger.info(
            f"Component Called: {symbol}{event.ctx.invoke_target} with {event.ctx.args = } | {event.ctx.kwargs = }"
        )

    @Listener.create(is_default_listener=True)
    async def on_autocomplete_error(self, event: events.AutocompleteError) -> None:
        """
        Catches all errors dispatched by autocompletion options.

        By default it will dispatch the `Error` event.

        Listen to the `AutocompleteError` event to overwrite this behaviour.

        """
        self.dispatch(
            events.Error(
                source=f"Autocomplete Callback for /{event.ctx.invoke_target} - Option: {event.ctx.focussed_option}",
                error=event.error,
                args=event.args,
                kwargs=event.kwargs,
                ctx=event.ctx,
            )
        )

    @Listener.create(is_default_listener=True)
    async def on_autocomplete_completion(self, event: events.AutocompleteCompletion) -> None:
        """
        Called *after* any autocomplete callback is ran.

        By default, it will simply log the autocomplete callback.

        Listen to the `AutocompleteCompletion` event to overwrite this behaviour.

        """
        symbol = "$"
        self.logger.info(
            f"Autocomplete Called: {symbol}{event.ctx.invoke_target} with {event.ctx.focussed_option = } | {event.ctx.kwargs = }"
        )

    @Listener.create(is_default_listener=True)
    async def on_modal_error(self, event: events.ModalError) -> None:
        """
        Catches all errors dispatched by modals.

        By default it will dispatch the `Error` event.

        Listen to the `ModalError` event to overwrite this behaviour.

        """
        self.dispatch(
            events.Error(
                source=f"Modal Callback for custom_id {event.ctx.custom_id}",
                error=event.error,
                args=event.args,
                kwargs=event.kwargs,
                ctx=event.ctx,
            )
        )

    @Listener.create(is_default_listener=True)
    async def on_modal_completion(self, event: events.ModalCompletion) -> None:
        """
        Called *after* any modal callback is ran.

        By default, it will simply log the modal callback.

        Listen to the `ModalCompletion` event to overwrite this behaviour.

        """
        self.logger.info(f"Modal Called: {event.ctx.custom_id = } with {event.ctx.responses = }")

    @Listener.create()
    async def on_resume(self) -> None:
        self._ready.set()

    @Listener.create(is_default_listener=True)
    async def _on_websocket_ready(self, event: events.RawGatewayEvent) -> None:
        """
        Catches websocket ready and determines when to dispatch the client `READY` signal.

        Args:
            event: The websocket ready packet

        """
        data = event.data
        expected_guilds = {to_snowflake(guild["id"]) for guild in data["guilds"]}
        self._user._add_guilds(expected_guilds)

        if not self._startup:
            while True:
                try:  # wait to let guilds cache
                    await asyncio.wait_for(self._guild_event.wait(), self.guild_event_timeout)
                except asyncio.TimeoutError:
                    # this will *mostly* occur when a guild has been shadow deleted by discord T&S.
                    # there is no way to check for this, so we just need to wait for this to time out.
                    # We still log it though, just in case.
                    self.logger.debug("Timeout waiting for guilds cache")
                    break
                self._guild_event.clear()

                if len(self.cache.guild_cache) == len(expected_guilds):
                    # all guilds cached
                    break

            if self.fetch_members:
                # ensure all guilds have completed chunking
                for guild in self.guilds:
                    if guild and not guild.chunked.is_set():
                        self.logger.debug(f"Waiting for {guild.id} to chunk")
                        await guild.chunked.wait()

            # cache slash commands
            if not self._startup:
                await self._init_interactions()

            self._startup = True
            self.dispatch(events.Startup())

        else:
            # reconnect ready
            ready_guilds = set()

            async def _temp_listener(_event: events.RawGatewayEvent) -> None:
                ready_guilds.add(_event.data["id"])

            listener = Listener.create("_on_raw_guild_create")(_temp_listener)
            self.add_listener(listener)

            while True:
                try:
                    await asyncio.wait_for(self._guild_event.wait(), self.guild_event_timeout)
                    if len(ready_guilds) == len(expected_guilds):
                        break
                except asyncio.TimeoutError:
                    break

            self.listeners["raw_guild_create"].remove(listener)

        self._ready.set()
        self.dispatch(events.Ready())

    async def login(self, token) -> None:
        """
        Login to discord via http.

        !!! note
            You will need to run Naff.start_gateway() before you start receiving gateway events.

        Args:
            token str: Your bot's token

        """
        # i needed somewhere to put this call,
        # login will always run after initialisation
        # so im gathering commands here
        self._gather_commands()

        self.logger.debug("Attempting to login")
        me = await self.http.login(token.strip())
        self._user = NaffUser.from_dict(me, self)
        self.cache.place_user_data(me)
        self._app = Application.from_dict(await self.http.get_current_bot_information(), self)
        self._mention_reg = re.compile(rf"^(<@!?{self.user.id}*>\s)")

        if self.app.owner:
            self.owner_ids.add(self.app.owner.id)

        self.dispatch(events.Login())

    async def astart(self, token: str) -> None:
        """
        Asynchronous method to start the bot.

        Args:
            token: Your bot's token
        """
        await self.login(token)

        # run any pending startup tasks
        if self.async_startup_tasks:
            try:
                await asyncio.gather(*self.async_startup_tasks)
            except Exception as e:
                self.dispatch(events.Error(source="async-extension-loader", error=e))
        try:
            await self._connection_state.start()
        finally:
            await self.stop()

    def start(self, token: str) -> None:
        """
        Start the bot.

        info:
            This is the recommended method to start the bot
        """
        try:
            asyncio.run(self.astart(token))
        except KeyboardInterrupt:
            # ignore, cus this is useless and can be misleading to the
            # user
            pass

    async def start_gateway(self) -> None:
        """Starts the gateway connection."""
        try:
            await self._connection_state.start()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shutdown the bot."""
        self.logger.debug("Stopping the bot.")
        self._ready.clear()
        await self.http.close()
        await self._connection_state.stop()

    def dispatch(self, event: events.BaseEvent, *args, **kwargs) -> None:
        """
        Dispatch an event.

        Args:
            event: The event to be dispatched.

        """
        listeners = self.listeners.get(event.resolved_name, [])
        if listeners:
            self.logger.debug(f"Dispatching Event: {event.resolved_name}")
            event.bot = self
            for _listen in listeners:
                try:
                    self._queue_task(_listen, event, *args, **kwargs)
                except Exception as e:
                    raise BotException(
                        f"An error occurred attempting during {event.resolved_name} event processing"
                    ) from e

        _waits = self.waits.get(event.resolved_name, [])
        if _waits:
            index_to_remove = []
            for i, _wait in enumerate(_waits):
                result = _wait(event)
                if result:
                    index_to_remove.append(i)

            for idx in sorted(index_to_remove, reverse=True):
                _waits.pop(idx)

    async def wait_until_ready(self) -> None:
        """Waits for the client to become ready."""
        await self._ready.wait()

    def wait_for(
        self,
        event: Union[str, "BaseEvent"],
        checks: Absent[Optional[Callable[..., bool]]] = MISSING,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Waits for a WebSocket event to be dispatched.

        Args:
            event: The name of event to wait.
            checks: A predicate to check what to wait for.
            timeout: The number of seconds to wait before timing out.

        Returns:
            The event object.

        """
        event = get_event_name(event)

        if event not in self.waits:
            self.waits[event] = []

        future = asyncio.Future()
        self.waits[event].append(Wait(event, checks, future))

        return asyncio.wait_for(future, timeout)

    async def wait_for_modal(
        self,
        modal: "Modal",
        author: Optional["Snowflake_Type"] = None,
        timeout: Optional[float] = None,
    ) -> ModalContext:
        """
        Wait for a modal response.

        Args:
            modal: The modal we're waiting for.
            author: The user we're waiting for to reply
            timeout: A timeout in seconds to stop waiting

        Returns:
            The context of the modal response

        Raises:
            asyncio.TimeoutError: if no response is received that satisfies the predicate before timeout seconds have passed

        """
        author = to_snowflake(author) if author else None

        def predicate(event) -> bool:
            if modal.custom_id != event.ctx.custom_id:
                return False
            if author and author != to_snowflake(event.ctx.author):
                return False
            return True

        resp = await self.wait_for("modal_completion", predicate, timeout)
        return resp.ctx

    async def wait_for_component(
        self,
        messages: Union[Message, int, list] = None,
        components: Optional[
            Union[List[List[Union["BaseComponent", dict]]], List[Union["BaseComponent", dict]], "BaseComponent", dict]
        ] = None,
        check: Optional[Callable] = None,
        timeout: Optional[float] = None,
    ) -> "Component":
        """
        Waits for a component to be sent to the bot.

        Args:
            messages: The message object to check for.
            components: The components to wait for.
            check: A predicate to check what to wait for.
            timeout: The number of seconds to wait before timing out.

        Returns:
            `Component` that was invoked. Use `.ctx` to get the `ComponentContext`.

        Raises:
            asyncio.TimeoutError: if timed out

        """
        if not (messages or components):
            raise ValueError("You must specify messages or components (or both)")

        message_ids = (
            to_snowflake_list(messages) if isinstance(messages, list) else to_snowflake(messages) if messages else None
        )
        custom_ids = list(get_components_ids(components)) if components else None

        # automatically convert improper custom_ids
        if custom_ids and not all(isinstance(x, str) for x in custom_ids):
            custom_ids = [str(i) for i in custom_ids]

        def _check(event: Component) -> bool:
            ctx: ComponentContext = event.ctx
            # if custom_ids is empty or there is a match
            wanted_message = not message_ids or ctx.message.id in (
                [message_ids] if isinstance(message_ids, int) else message_ids
            )
            wanted_component = not custom_ids or ctx.custom_id in custom_ids
            if wanted_message and wanted_component:
                if check is None or check(event):
                    return True
                return False
            return False

        return await self.wait_for("component", checks=_check, timeout=timeout)

    def listen(self, event_name: Absent[str] = MISSING) -> Listener:
        """
        A decorator to be used in situations that Naff can't automatically hook your listeners. Ideally, the standard listen decorator should be used, not this.

        Args:
            event_name: The event name to use, if not the coroutine name

        Returns:
            A listener that can be used to hook into the event.

        """

        def wrapper(coro: Callable[..., Coroutine]) -> Listener:
            listener = Listener.create(event_name)(coro)
            self.add_listener(listener)
            return listener

        return wrapper

    def add_event_processor(self, event_name: Absent[str] = MISSING) -> Callable[..., Coroutine]:
        """
        A decorator to be used to add event processors.

        Args:
            event_name: The event name to use, if not the coroutine name

        Returns:
            A function that can be used to hook into the event.

        """

        def wrapper(coro: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
            name = event_name
            if name is MISSING:
                name = coro.__name__
            name = name.lstrip("_")
            name = name.removeprefix("on_")
            self.processors[name] = coro
            return coro

        return wrapper

    def add_listener(self, listener: Listener) -> None:
        """
        Add a listener for an event, if no event is passed, one is determined.

        Args:
            listener Listener: The listener to add to the client

        """
        if not listener.is_default_listener:
            # check that the required intents are enabled

            event_class_name = "".join([name.capitalize() for name in listener.event.split("_")])
            if event_class := globals().get(event_class_name):
                if required_intents := _INTENT_EVENTS.get(event_class):  # noqa
                    if not any(required_intent in self.intents for required_intent in required_intents):
                        self.logger.warning(
                            f"Event `{listener.event}` will not work since the required intent is not set -> Requires any of: `{required_intents}`"
                        )

        if listener.event not in self.listeners:
            self.listeners[listener.event] = []
        self.listeners[listener.event].append(listener)

        # check if other listeners are to be deleted
        default_listeners = [c_listener.is_default_listener for c_listener in self.listeners[listener.event]]
        removes_defaults = [c_listener.disable_default_listeners for c_listener in self.listeners[listener.event]]

        if any(default_listeners) and any(removes_defaults):
            self.listeners[listener.event] = [
                c_listener for c_listener in self.listeners[listener.event] if not c_listener.is_default_listener
            ]

    def add_interaction(self, command: InteractionCommand) -> bool:
        """
        Add a slash command to the client.

        Args:
            command InteractionCommand: The command to add

        """
        if self.debug_scope:
            command.scopes = [self.debug_scope]

        if self.disable_dm_commands:
            command.dm_permission = False

        # for SlashCommand objs without callback (like objects made to hold group info etc)
        if command.callback is None:
            return False

        base, group, sub, *_ = command.resolved_name.split(" ") + [None, None]

        for scope in command.scopes:
            if scope not in self.interactions:
                self.interactions[scope] = {}
            elif command.resolved_name in self.interactions[scope]:
                old_cmd = self.interactions[scope][command.resolved_name]
                raise ValueError(f"Duplicate Command! {scope}::{old_cmd.resolved_name}")

            if self.enforce_interaction_perms:
                command.checks.append(command._permission_enforcer)  # noqa : w0212

            self.interactions[scope][command.resolved_name] = command

            if scope not in self.interaction_tree:
                self.interaction_tree[scope] = {}

            if group is None or isinstance(command, ContextMenu):
                self.interaction_tree[scope][command.resolved_name] = command
            elif group is not None:
                if not (current := self.interaction_tree[scope].get(base)) or isinstance(current, SlashCommand):
                    self.interaction_tree[scope][base] = {}
                if sub is None:
                    self.interaction_tree[scope][base][group] = command
                else:
                    if not (current := self.interaction_tree[scope][base].get(group)) or isinstance(
                        current, SlashCommand
                    ):
                        self.interaction_tree[scope][base][group] = {}
                    self.interaction_tree[scope][base][group][sub] = command

        return True

    def add_hybrid_command(self, command: HybridCommand) -> bool:
        if self.debug_scope:
            command.scopes = [self.debug_scope]

        if command.callback is None:
            return False

        if command.is_subcommand:
            prefixed_base = self.prefixed_commands.get(str(command.name))
            if not prefixed_base:
                prefixed_base = _base_subcommand_generator(
                    str(command.name), list(command.name.to_locale_dict().values()), str(command.description)
                )
                self.add_prefixed_command(prefixed_base)

            if command.group_name:  # if this is a group command
                _prefixed_cmd = prefixed_base
                prefixed_base = prefixed_base.subcommands.get(str(command.group_name))

                if not prefixed_base:
                    prefixed_base = _base_subcommand_generator(
                        str(command.group_name),
                        list(command.group_name.to_locale_dict().values()),
                        str(command.group_description),
                        group=True,
                    )
                    _prefixed_cmd.add_command(prefixed_base)

            new_command = _prefixed_from_slash(command)
            new_command._parse_parameters()
            prefixed_base.add_command(new_command)
        else:
            new_command = _prefixed_from_slash(command)
            self.add_prefixed_command(new_command)

        return self.add_interaction(command)

    def add_prefixed_command(self, command: PrefixedCommand) -> None:
        """
        Add a prefixed command to the client.

        Args:
            command PrefixedCommand: The command to add

        """
        # check that the required intent is enabled or the prefix is a mention
        prefixes = (
            self.default_prefix
            if not isinstance(self.default_prefix, str) and not self.default_prefix == MENTION_PREFIX
            else (self.default_prefix,)
        )
        if (MENTION_PREFIX not in prefixes) and (Intents.GUILD_MESSAGE_CONTENT not in self.intents):
            self.logger.warning(
                f"Prefixed commands will not work since the required intent is not set -> Requires: `{Intents.GUILD_MESSAGE_CONTENT.__repr__()}` or usage of the default `MENTION_PREFIX` as the prefix"
            )

        command._parse_parameters()

        if self.prefixed_commands.get(command.name):
            raise ValueError(f"Duplicate command! Multiple commands share the name/alias: {command.name}.")
        self.prefixed_commands[command.name] = command

        for alias in command.aliases:
            if self.prefixed_commands.get(alias):
                raise ValueError(f"Duplicate command! Multiple commands share the name/alias: {alias}.")
            self.prefixed_commands[alias] = command

    def add_component_callback(self, command: ComponentCommand) -> None:
        """
        Add a component callback to the client.

        Args:
            command: The command to add

        """
        for listener in command.listeners:
            # I know this isn't an ideal solution, but it means we can lookup callbacks with O(1)
            if listener not in self._component_callbacks.keys():
                self._component_callbacks[listener] = command
                continue
            else:
                raise ValueError(f"Duplicate Component! Multiple component callbacks for `{listener}`")

    def add_modal_callback(self, command: ModalCommand) -> None:
        """
        Add a modal callback to the client.

        Args:
            command: The command to add
        """
        for listener in command.listeners:
            if listener not in self._modal_callbacks.keys():
                self._modal_callbacks[listener] = command
                continue
            else:
                raise ValueError(f"Duplicate Component! Multiple modal callbacks for `{listener}`")

    def _gather_commands(self) -> None:
        """Gathers commands from __main__ and self."""

        def process(_cmds) -> None:

            for func in _cmds:
                if isinstance(func, ModalCommand):
                    self.add_modal_callback(func)
                elif isinstance(func, ComponentCommand):
                    self.add_component_callback(func)
                elif isinstance(func, HybridCommand):
                    self.add_hybrid_command(func)
                elif isinstance(func, InteractionCommand):
                    self.add_interaction(func)
                elif (
                    isinstance(func, PrefixedCommand) and not func.is_subcommand
                ):  # subcommands will be added with main comamnds
                    self.add_prefixed_command(func)
                elif isinstance(func, Listener):
                    self.add_listener(func)

            self.logger.debug(f"{len(_cmds)} commands have been loaded from `__main__` and `client`")

        process(
            [obj for _, obj in inspect.getmembers(sys.modules["__main__"]) if isinstance(obj, (BaseCommand, Listener))]
        )
        process(
            [
                obj.copy_with_binding(self)
                for _, obj in inspect.getmembers(self)
                if isinstance(obj, (BaseCommand, Listener))
            ]
        )

        [wrap_partial(obj, self) for _, obj in inspect.getmembers(self) if isinstance(obj, Task)]

    async def _init_interactions(self) -> None:
        """
        Initialise slash commands.

        If `sync_interactions` this will submit all registered slash
        commands to discord. Otherwise, it will get the list of
        interactions and cache their scopes.

        """
        # allow for ext and main to share the same decorator
        try:
            if self.sync_interactions:
                await self.synchronise_interactions()
            else:
                await self._cache_interactions(warn_missing=False)
        except Exception as e:
            self.dispatch(events.Error(source="Interaction Syncing", error=e))

    async def _cache_interactions(self, warn_missing: bool = False) -> None:
        """Get all interactions used by this bot and cache them."""
        if warn_missing or self.del_unused_app_cmd:
            bot_scopes = {g.id for g in self.cache.guild_cache.values()}
            bot_scopes.add(GLOBAL_SCOPE)
        else:
            bot_scopes = set(self.interactions)

        req_lock = asyncio.Lock()

        async def wrap(*args, **kwargs) -> Absent[List[Dict]]:
            async with req_lock:
                # throttle this
                await asyncio.sleep(0.1)
            try:
                return await self.http.get_application_commands(*args, **kwargs)
            except Forbidden:
                return MISSING

        results = await asyncio.gather(*[wrap(self.app.id, scope) for scope in bot_scopes])
        results = dict(zip(bot_scopes, results))

        for scope, remote_cmds in results.items():
            if remote_cmds == MISSING:
                self.logger.debug(f"Bot was not invited to guild {scope} with `application.commands` scope")
                continue

            remote_cmds = {cmd_data["name"]: cmd_data for cmd_data in remote_cmds}
            found = set()  # this is a temporary hack to fix subcommand detection
            if scope in self.interactions:
                for cmd in self.interactions[scope].values():
                    cmd_name = str(cmd.name)
                    cmd_data = remote_cmds.get(cmd_name, MISSING)
                    if cmd_data is MISSING:
                        if cmd_name not in found:
                            if warn_missing:
                                self.logger.error(
                                    f'Detected yet to sync slash command "/{cmd_name}" for scope '
                                    f"{'global' if scope == GLOBAL_SCOPE else scope}"
                                )
                        continue
                    else:
                        found.add(cmd_name)
                    self._interaction_scopes[str(cmd_data["id"])] = scope
                    cmd.cmd_id[scope] = int(cmd_data["id"])

            if warn_missing:
                for cmd_data in remote_cmds.values():
                    self.logger.error(
                        f"Detected unimplemented slash command \"/{cmd_data['name']}\" for scope "
                        f"{'global' if scope == GLOBAL_SCOPE else scope}"
                    )

    async def synchronise_interactions(
        self, *, scopes: Sequence["Snowflake_Type"] = MISSING, delete_commands: Absent[bool] = MISSING
    ) -> None:
        """
        Synchronise registered interactions with discord.

        Args:
            scopes: Optionally specify which scopes are to be synced
            delete_commands: Override the client setting and delete commands
        """
        s = time.perf_counter()
        _delete_cmds = self.del_unused_app_cmd if delete_commands is MISSING else delete_commands
        await self._cache_interactions()

        if scopes is not MISSING:
            cmd_scopes = scopes
        elif self.del_unused_app_cmd:
            # if we're deleting unused commands, we check all scopes
            cmd_scopes = [to_snowflake(g_id) for g_id in self._user._guild_ids] + [GLOBAL_SCOPE]
        else:
            # if we're not deleting, just check the scopes we have cmds registered in
            cmd_scopes = list(set(self.interactions) | {GLOBAL_SCOPE})

        local_cmds_json = application_commands_to_dict(self.interactions, self)

        async def sync_scope(cmd_scope) -> None:

            sync_needed_flag = False  # a flag to force this scope to synchronise
            sync_payload = []  # the payload to be pushed to discord

            try:
                try:
                    remote_commands = await self.http.get_application_commands(self.app.id, cmd_scope)
                except Forbidden:
                    self.logger.warning(f"Bot is lacking `application.commands` scope in {cmd_scope}!")
                    return

                for local_cmd in self.interactions.get(cmd_scope, {}).values():
                    # get remote equivalent of this command
                    remote_cmd_json = next(
                        (v for v in remote_commands if int(v["id"]) == local_cmd.cmd_id.get(cmd_scope)), None
                    )
                    # get json representation of this command
                    local_cmd_json = next((c for c in local_cmds_json[cmd_scope] if c["name"] == str(local_cmd.name)))

                    # this works by adding any command we *want* on Discord, to a payload, and synchronising that
                    # this allows us to delete unused commands, add new commands, or do nothing in 1 or less API calls

                    if sync_needed(local_cmd_json, remote_cmd_json):
                        # determine if the local and remote commands are out-of-sync
                        sync_needed_flag = True
                        sync_payload.append(local_cmd_json)
                    elif not _delete_cmds and remote_cmd_json:
                        _remote_payload = {
                            k: v for k, v in remote_cmd_json.items() if k not in ("id", "application_id", "version")
                        }
                        sync_payload.append(_remote_payload)
                    elif _delete_cmds:
                        sync_payload.append(local_cmd_json)

                sync_payload = [json.loads(_dump) for _dump in {json.dumps(_cmd) for _cmd in sync_payload}]

                if sync_needed_flag or (_delete_cmds and len(sync_payload) < len(remote_commands)):
                    # synchronise commands if flag is set, or commands are to be deleted
                    self.logger.info(f"Overwriting {cmd_scope} with {len(sync_payload)} application commands")
                    sync_response: list[dict] = await self.http.overwrite_application_commands(
                        self.app.id, sync_payload, cmd_scope
                    )
                    self._cache_sync_response(sync_response, cmd_scope)
                else:
                    self.logger.debug(f"{cmd_scope} is already up-to-date with {len(remote_commands)} commands.")

            except Forbidden as e:
                raise InteractionMissingAccess(cmd_scope) from e
            except HTTPException as e:
                self._raise_sync_exception(e, local_cmds_json, cmd_scope)

        await asyncio.gather(*[sync_scope(scope) for scope in cmd_scopes])

        t = time.perf_counter() - s
        self.logger.debug(f"Sync of {len(cmd_scopes)} scopes took {t} seconds")

    def get_application_cmd_by_id(self, cmd_id: "Snowflake_Type") -> Optional[InteractionCommand]:
        """
        Get a application command from the internal cache by its ID.

        Args:
            cmd_id: The ID of the command

        Returns:
            The command, if one with the given ID exists internally, otherwise None

        """
        scope = self._interaction_scopes.get(str(cmd_id), MISSING)
        cmd_id = int(cmd_id)  # ensure int ID
        if scope != MISSING:
            for cmd in self.interactions[scope].values():
                if int(cmd.cmd_id.get(scope)) == cmd_id:
                    return cmd
        return None

    def _raise_sync_exception(self, e: HTTPException, cmds_json: dict, cmd_scope: "Snowflake_Type") -> NoReturn:
        try:
            if isinstance(e.errors, dict):
                for cmd_num in e.errors.keys():
                    cmd = cmds_json[cmd_scope][int(cmd_num)]
                    output = e.search_for_message(e.errors[cmd_num], cmd)
                    if len(output) > 1:
                        output = "\n".join(output)
                        self.logger.error(f"Multiple Errors found in command `{cmd['name']}`:\n{output}")
                    else:
                        self.logger.error(f"Error in command `{cmd['name']}`: {output[0]}")
            else:
                raise e from None
        except Exception:
            # the above shouldn't fail, but if it does, just raise the exception normally
            raise e from None

    def _cache_sync_response(self, sync_response: list[dict], scope: "Snowflake_Type") -> None:
        for cmd_data in sync_response:
            self._interaction_scopes[cmd_data["id"]] = scope
            if cmd_data["name"] in self.interactions[scope]:
                self.interactions[scope][cmd_data["name"]].cmd_id[scope] = int(cmd_data["id"])
            else:
                # sub_cmd
                for sc in cmd_data["options"]:
                    if sc["type"] == OptionTypes.SUB_COMMAND:
                        if f"{cmd_data['name']} {sc['name']}" in self.interactions[scope]:
                            self.interactions[scope][f"{cmd_data['name']} {sc['name']}"].cmd_id[scope] = int(
                                cmd_data["id"]
                            )
                    elif sc["type"] == OptionTypes.SUB_COMMAND_GROUP:
                        for _sc in sc["options"]:
                            if f"{cmd_data['name']} {sc['name']} {_sc['name']}" in self.interactions[scope]:
                                self.interactions[scope][f"{cmd_data['name']} {sc['name']} {_sc['name']}"].cmd_id[
                                    scope
                                ] = int(cmd_data["id"])

    @overload
    async def get_context(self, data: ComponentChannelInteractionData, interaction: Literal[True]) -> ComponentContext:
        ...

    @overload
    async def get_context(
        self, data: AutocompleteChannelInteractionData, interaction: Literal[True]
    ) -> AutocompleteContext:
        ...

    # as of right now, discord_typings doesn't include anything like this
    # @overload
    # async def get_context(self, data: ModalSubmitInteractionData, interaction: Literal[True]) -> ModalContext:
    #     ...

    @overload
    async def get_context(self, data: InteractionData, interaction: Literal[True]) -> InteractionContext:
        ...

    @overload
    async def get_context(
        self, data: dict, interaction: Literal[True]
    ) -> ComponentContext | AutocompleteContext | ModalContext | InteractionContext:
        # fallback case since some data isn't typehinted properly
        ...

    @overload
    async def get_context(self, data: Message, interaction: Literal[False] = False) -> PrefixedContext:
        ...

    async def get_context(
        self, data: InteractionData | dict | Message, interaction: bool = False
    ) -> ComponentContext | AutocompleteContext | ModalContext | InteractionContext | PrefixedContext:
        """
        Return a context object based on data passed.

        !!! note
            If you want to use custom context objects, this is the method to override. Your replacement must take the same arguments as this, and return a Context-like object.

        Args:
            data: The data of the event
            interaction: Is this an interaction or not?

        Returns:
            Context object

        """
        # this line shuts up IDE warnings
        cls: ComponentContext | AutocompleteContext | ModalContext | InteractionContext | PrefixedContext

        if interaction:
            match data["type"]:
                case InteractionTypes.MESSAGE_COMPONENT:
                    cls = self.component_context.from_dict(data, self)

                case InteractionTypes.AUTOCOMPLETE:
                    cls = self.autocomplete_context.from_dict(data, self)

                case InteractionTypes.MODAL_RESPONSE:
                    cls = self.modal_context.from_dict(data, self)

                case _:
                    cls = self.interaction_context.from_dict(data, self)

            if not cls.channel:
                try:
                    cls.channel = await self.cache.fetch_channel(data["channel_id"])
                except Forbidden:
                    cls.channel = BaseChannel.from_dict_factory(
                        {"id": data["channel_id"], "type": ChannelTypes.GUILD_TEXT}, self
                    )

        else:
            cls = self.prefixed_context.from_message(self, data)
            if not cls.channel:
                cls.channel = await self.cache.fetch_channel(data._channel_id)

        return cls

    async def _run_slash_command(self, command: SlashCommand, ctx: InteractionContext) -> Any:
        """Overrideable method that executes slash commands, can be used to wrap callback execution"""
        return await command(ctx, **ctx.kwargs)

    async def _run_prefixed_command(self, command: PrefixedCommand, ctx: PrefixedContext) -> Any:
        """Overrideable method that executes prefixed commands, can be used to wrap callback execution"""
        return await command(ctx)

    @processors.Processor.define("raw_interaction_create")
    async def _dispatch_interaction(self, event: RawGatewayEvent) -> None:
        """
        Identify and dispatch interaction of slash commands or components.

        Args:
            raw interaction event

        """
        interaction_data = event.data

        if interaction_data["type"] in (
            InteractionTypes.PING,
            InteractionTypes.APPLICATION_COMMAND,
            InteractionTypes.AUTOCOMPLETE,
        ):
            interaction_id = interaction_data["data"]["id"]
            name = interaction_data["data"]["name"]
            scope = self._interaction_scopes.get(str(interaction_id))

            if scope in self.interactions:
                ctx = await self.get_context(interaction_data, True)

                ctx.command: SlashCommand = self.interactions[scope][ctx.invoke_target]  # type: ignore
                self.logger.debug(f"{scope} :: {ctx.command.name} should be called")

                if ctx.command.auto_defer:
                    auto_defer = ctx.command.auto_defer
                elif ctx.command.extension and ctx.command.extension.auto_defer:
                    auto_defer = ctx.command.extension.auto_defer
                else:
                    auto_defer = self.auto_defer

                if auto_opt := getattr(ctx, "focussed_option", None):
                    try:
                        await ctx.command.autocomplete_callbacks[auto_opt](ctx, **ctx.kwargs)
                    except Exception as e:
                        self.dispatch(events.AutocompleteError(ctx=ctx, error=e))
                    finally:
                        self.dispatch(events.AutocompleteCompletion(ctx=ctx))
                else:
                    try:
                        await auto_defer(ctx)
                        if self.pre_run_callback:
                            await self.pre_run_callback(ctx, **ctx.kwargs)
                        await self._run_slash_command(ctx.command, ctx)
                        if self.post_run_callback:
                            await self.post_run_callback(ctx, **ctx.kwargs)
                    except Exception as e:
                        self.dispatch(events.CommandError(ctx=ctx, error=e))
                    finally:
                        self.dispatch(events.CommandCompletion(ctx=ctx))
            else:
                self.logger.error(f"Unknown cmd_id received:: {interaction_id} ({name})")

        elif interaction_data["type"] == InteractionTypes.MESSAGE_COMPONENT:
            # Buttons, Selects, ContextMenu::Message
            ctx = await self.get_context(interaction_data, True)
            component_type = interaction_data["data"]["component_type"]

            self.dispatch(events.Component(ctx=ctx))
            if callback := self._component_callbacks.get(ctx.custom_id):
                ctx.command = callback
                try:
                    if self.pre_run_callback:
                        await self.pre_run_callback(ctx)
                    await callback(ctx)
                    if self.post_run_callback:
                        await self.post_run_callback(ctx)
                except Exception as e:
                    self.dispatch(events.ComponentError(ctx=ctx, error=e))
                finally:
                    self.dispatch(events.ComponentCompletion(ctx=ctx))
            if component_type == ComponentTypes.BUTTON:
                self.dispatch(events.ButtonPressed(ctx))
            if component_type == ComponentTypes.STRING_SELECT:
                self.dispatch(events.Select(ctx))

        elif interaction_data["type"] == InteractionTypes.MODAL_RESPONSE:
            ctx = await self.get_context(interaction_data, True)
            self.dispatch(events.ModalCompletion(ctx=ctx))

            # todo: Polls remove this icky code duplication - love from past-polls ❤️
            if callback := self._modal_callbacks.get(ctx.custom_id):
                ctx.command = callback

                try:
                    if self.pre_run_callback:
                        await self.pre_run_callback(ctx)
                    await callback(ctx)
                    if self.post_run_callback:
                        await self.post_run_callback(ctx)
                except Exception as e:
                    self.dispatch(events.ModalError(ctx=ctx, error=e))

        else:
            raise NotImplementedError(f"Unknown Interaction Received: {interaction_data['type']}")

    @Listener.create("raw_message_create", is_default_listener=True)
    async def _dispatch_prefixed_commands(self, event: RawGatewayEvent) -> None:
        """Determine if a prefixed command is being triggered, and dispatch it."""
        # don't waste time processing this if there are no prefixed commands
        if not self.prefixed_commands:
            return

        data = event.data

        # many bots will not have the message content intent, and so will not have content
        # for most messages. since there's nothing for prefixed commands to work off of,
        # we might as well not waste time
        if not data.get("content"):
            return

        # webhooks and users labeled with the bot property are bots, and should be ignored
        if data.get("webhook_id") or data["author"].get("bot", False):
            return

        # now, we've done the basic filtering out, but everything from here on out relies
        # on a proper message object, so now we either hope its already in the cache or wait
        # on the processor

        # first, let's check the cache...
        message = self.cache.get_message(int(data["channel_id"]), int(data["id"]))

        # this huge if statement basically checks if the message hasn't been fully processed by
        # the processor yet, which would mean that these fields aren't fully filled
        if message and (
            (not message._guild_id and event.data.get("guild_id"))
            or (message._guild_id and not message.guild)
            or not message.channel
        ):
            message = None

        # if we didn't get a message, then we know we should wait for the message create event
        if not message:
            try:
                # i think 2 seconds is a very generous timeout limit
                event: MessageCreate = await self.wait_for(
                    MessageCreate, checks=lambda e: int(e.message.id) == int(data["id"]), timeout=2
                )
                message = event.message
            except asyncio.TimeoutError:
                return

        # here starts the actual prefixed command parsing part
        prefixes: str | Iterable[str] = await self.generate_prefixes(self, message)

        if isinstance(prefixes, str) or prefixes == MENTION_PREFIX:
            # its easier to treat everything as if it may be an iterable
            # rather than building a special case for this
            prefixes = (prefixes,)  # type: ignore

        prefix_used = None

        for prefix in prefixes:
            if prefix == MENTION_PREFIX:
                if mention := self._mention_reg.search(message.content):  # type: ignore
                    prefix = mention.group()
                else:
                    continue

            if message.content.startswith(prefix):
                prefix_used = prefix
                break

        if not prefix_used:
            return

        context = await self.get_context(message)
        context.prefix = prefix_used

        # interestingly enough, we cannot count on ctx.invoke_target
        # being correct as its hard to account for newlines and the like
        # with the way we get subcommands here
        # we'll have to reconstruct it by getting the content_parameters
        # then removing the prefix and the parameters from the message
        # content
        content_parameters = message.content.removeprefix(prefix_used)  # type: ignore
        command: "Client | PrefixedCommand" = self  # yes, this is a hack

        while True:
            first_word: str = get_first_word(content_parameters)  # type: ignore
            if isinstance(command, PrefixedCommand):
                new_command = command.subcommands.get(first_word)
            else:
                new_command = command.prefixed_commands.get(first_word)
            if not new_command or not new_command.enabled:
                break

            command = new_command
            content_parameters = content_parameters.removeprefix(first_word).strip()

            if command.subcommands and command.hierarchical_checking:
                try:
                    await new_command._can_run(context)  # will error out if we can't run this command
                except Exception as e:
                    if new_command.error_callback:
                        await new_command.error_callback(e, context)
                    elif new_command.extension and new_command.extension.extension_error:
                        await new_command.extension.extension_error(e, context)
                    else:
                        self.dispatch(events.CommandError(ctx=context, error=e))
                    return

        if not isinstance(command, PrefixedCommand) or not command.enabled:
            return

        context.command = command
        context.invoke_target = message.content.removeprefix(prefix_used).removesuffix(content_parameters).strip()  # type: ignore
        context.args = get_args(context.content_parameters)
        try:
            if self.pre_run_callback:
                await self.pre_run_callback(context)
            await self._run_prefixed_command(command, context)
            if self.post_run_callback:
                await self.post_run_callback(context)
        except Exception as e:
            self.dispatch(events.CommandError(ctx=context, error=e))
        finally:
            self.dispatch(events.CommandCompletion(ctx=context))

    @Listener.create("disconnect", is_default_listener=True)
    async def _disconnect(self) -> None:
        self._ready.clear()

    def get_extensions(self, name: str) -> list[Extension]:
        """
        Get all ext with a name or extension name.

        Args:
            name: The name of the extension, or the name of it's extension

        Returns:
            List of Extensions
        """
        if name not in self.ext.keys():
            return [ext for ext in self.ext.values() if ext.extension_name == name]

        return [self.ext.get(name, None)]

    def get_ext(self, name: str) -> Extension | None:
        """
        Get a extension with a name or extension name.

        Args:
            name: The name of the extension, or the name of it's extension

        Returns:
            A extension, if found
        """
        if ext := self.get_extensions(name):
            return ext[0]
        return None

    def load_extension(self, name: str, package: str | None = None, **load_kwargs: Any) -> None:
        """
        Load an extension with given arguments.

        Args:
            name: The name of the extension.
            package: The package the extension is in
            **load_kwargs: The auto-filled mapping of the load keyword arguments

        """
        module_name = importlib.util.resolve_name(name, package)
        if module_name in self.__modules:
            raise Exception(f"{module_name} already loaded")

        module = importlib.import_module(module_name, package)
        try:
            setup = getattr(module, "setup", None)
            if setup:
                setup(self, **load_kwargs)
            else:
                self.logger.debug("No setup function found in %s", module_name)

                found = False
                objects = {name: obj for name, obj in inspect.getmembers(module) if isinstance(obj, type)}
                for obj_name, obj in objects.items():
                    if Extension in obj.__bases__:
                        self.logger.debug(f"Found extension class {obj_name} in {module_name}: Attempting to load")
                        obj(self, **load_kwargs)
                        found = True
                if not found:
                    raise Exception(f"{module_name} contains no Extensions")

        except ExtensionLoadException:
            raise
        except Exception as e:
            del sys.modules[module_name]
            raise ExtensionLoadException(f"Unexpected Error loading {module_name}") from e

        else:
            self.logger.debug(f"Loaded Extension: {module_name}")
            self.__modules[module_name] = module

            if self.sync_ext and self._ready.is_set():
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return
                asyncio.create_task(self.synchronise_interactions())

    def unload_extension(self, name: str, package: str | None = None, **unload_kwargs: Any) -> None:
        """
        Unload an extension with given arguments.

        Args:
            name: The name of the extension.
            package: The package the extension is in
            **unload_kwargs: The auto-filled mapping of the unload keyword arguments

        """
        name = importlib.util.resolve_name(name, package)
        module = self.__modules.get(name)

        if module is None:
            raise ExtensionNotFound(f"No extension called {name} is loaded")

        try:
            teardown = getattr(module, "teardown")
            teardown(**unload_kwargs)
        except AttributeError:
            pass

        for ext in self.get_extensions(name):
            ext.drop(**unload_kwargs)

        del sys.modules[name]
        del self.__modules[name]

        if self.sync_ext and self._ready.is_set():
            if self.sync_ext and self._ready.is_set():
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return
                asyncio.create_task(self.synchronise_interactions())

    def reload_extension(
        self,
        name: str,
        package: str | None = None,
        *,
        load_kwargs: Any = None,
        unload_kwargs: Any = None,
    ) -> None:
        """
        Helper method to reload an extension. Simply unloads, then loads the extension with given arguments.

        Args:
            name: The name of the extension.
            package: The package the extension is in
            load_kwargs: The manually-filled mapping of the load keyword arguments
            unload_kwargs: The manually-filled mapping of the unload keyword arguments

        """
        name = importlib.util.resolve_name(name, package)
        module = self.__modules.get(name)

        if module is None:
            self.logger.warning("Attempted to reload extension thats not loaded. Loading extension instead")
            return self.load_extension(name, package)

        if not load_kwargs:
            load_kwargs = {}
        if not unload_kwargs:
            unload_kwargs = {}

        self.unload_extension(name, package, **unload_kwargs)
        self.load_extension(name, package, **load_kwargs)

        # todo: maybe add an ability to revert to the previous version if unable to load the new one

    async def fetch_guild(self, guild_id: "Snowflake_Type") -> Optional[Guild]:
        """
        Fetch a guild.

        !!! note
            This method is an alias for the cache which will either return a cached object, or query discord for the object
            if its not already cached.

        Args:
            guild_id: The ID of the guild to get

        Returns:
            Guild Object if found, otherwise None

        """
        try:
            return await self.cache.fetch_guild(guild_id)
        except NotFound:
            return None

    def get_guild(self, guild_id: "Snowflake_Type") -> Optional[Guild]:
        """
        Get a guild.

        !!! note
            This method is an alias for the cache which will return a cached object.

        Args:
            guild_id: The ID of the guild to get

        Returns:
            Guild Object if found, otherwise None

        """
        return self.cache.get_guild(guild_id)

    async def create_guild_from_template(
        self,
        template_code: Union["GuildTemplate", str],
        name: str,
        icon: Absent[UPLOADABLE_TYPE] = MISSING,
    ) -> Optional[Guild]:
        """
        Creates a new guild based on a template.

        !!! note
            This endpoint can only be used by bots in less than 10 guilds.

        Args:
            template_code: The code of the template to use.
            name: The name of the guild (2-100 characters)
            icon: Location or File of icon to set

        Returns:
            The newly created guild object

        """
        if isinstance(template_code, GuildTemplate):
            template_code = template_code.code

        if icon:
            icon = to_image_data(icon)
        guild_data = await self.http.create_guild_from_guild_template(template_code, name, icon)
        return Guild.from_dict(guild_data, self)

    async def fetch_channel(self, channel_id: "Snowflake_Type") -> Optional["TYPE_ALL_CHANNEL"]:
        """
        Fetch a channel.

        !!! note
            This method is an alias for the cache which will either return a cached object, or query discord for the object
            if its not already cached.

        Args:
            channel_id: The ID of the channel to get

        Returns:
            Channel Object if found, otherwise None

        """
        try:
            return await self.cache.fetch_channel(channel_id)
        except NotFound:
            return None

    def get_channel(self, channel_id: "Snowflake_Type") -> Optional["TYPE_ALL_CHANNEL"]:
        """
        Get a channel.

        !!! note
            This method is an alias for the cache which will return a cached object.

        Args:
            channel_id: The ID of the channel to get

        Returns:
            Channel Object if found, otherwise None

        """
        return self.cache.get_channel(channel_id)

    async def fetch_user(self, user_id: "Snowflake_Type") -> Optional[User]:
        """
        Fetch a user.

        !!! note
            This method is an alias for the cache which will either return a cached object, or query discord for the object
            if its not already cached.

        Args:
            user_id: The ID of the user to get

        Returns:
            User Object if found, otherwise None

        """
        try:
            return await self.cache.fetch_user(user_id)
        except NotFound:
            return None

    def get_user(self, user_id: "Snowflake_Type") -> Optional[User]:
        """
        Get a user.

        !!! note
            This method is an alias for the cache which will return a cached object.

        Args:
            user_id: The ID of the user to get

        Returns:
            User Object if found, otherwise None

        """
        return self.cache.get_user(user_id)

    async def fetch_member(self, user_id: "Snowflake_Type", guild_id: "Snowflake_Type") -> Optional[Member]:
        """
        Fetch a member from a guild.

        !!! note
            This method is an alias for the cache which will either return a cached object, or query discord for the object
            if its not already cached.

        Args:
            user_id: The ID of the member
            guild_id: The ID of the guild to get the member from

        Returns:
            Member object if found, otherwise None

        """
        try:
            return await self.cache.fetch_member(guild_id, user_id)
        except NotFound:
            return None

    def get_member(self, user_id: "Snowflake_Type", guild_id: "Snowflake_Type") -> Optional[Member]:
        """
        Get a member from a guild.

        !!! note
            This method is an alias for the cache which will return a cached object.

        Args:
            user_id: The ID of the member
            guild_id: The ID of the guild to get the member from

        Returns:
            Member object if found, otherwise None

        """
        return self.cache.get_member(guild_id, user_id)

    async def fetch_scheduled_event(
        self, guild_id: "Snowflake_Type", scheduled_event_id: "Snowflake_Type", with_user_count: bool = False
    ) -> Optional["ScheduledEvent"]:
        """
        Fetch a scheduled event by id.

        Args:
            guild_id: The ID of the guild to get the scheduled event from
            scheduled_event_id: The ID of the scheduled event to get
            with_user_count: Whether to include the user count in the response

        Returns:
            The scheduled event if found, otherwise None

        """
        try:
            scheduled_event_data = await self.http.get_scheduled_event(guild_id, scheduled_event_id, with_user_count)
            return ScheduledEvent.from_dict(scheduled_event_data, self)
        except NotFound:
            return None

    async def fetch_custom_emoji(self, emoji_id: "Snowflake_Type", guild_id: "Snowflake_Type") -> Optional[CustomEmoji]:
        """
        Fetch a custom emoji by id.

        Args:
            emoji_id: The id of the custom emoji.
            guild_id: The id of the guild the emoji belongs to.

        Returns:
            The custom emoji if found, otherwise None.

        """
        try:
            return await self.cache.fetch_emoji(guild_id, emoji_id)
        except NotFound:
            return None

    def get_custom_emoji(
        self, emoji_id: "Snowflake_Type", guild_id: Optional["Snowflake_Type"] = None
    ) -> Optional[CustomEmoji]:
        """
        Get a custom emoji by id.

        Args:
            emoji_id: The id of the custom emoji.
            guild_id: The id of the guild the emoji belongs to.

        Returns:
            The custom emoji if found, otherwise None.

        """
        emoji = self.cache.get_emoji(emoji_id)
        if emoji and (not guild_id or emoji._guild_id == to_snowflake(guild_id)):
            return emoji
        return None

    async def fetch_sticker(self, sticker_id: "Snowflake_Type") -> Optional[Sticker]:
        """
        Fetch a sticker by ID.

        Args:
            sticker_id: The ID of the sticker.

        Returns:
            A sticker object if found, otherwise None

        """
        try:
            sticker_data = await self.http.get_sticker(sticker_id)
            return Sticker.from_dict(sticker_data, self)
        except NotFound:
            return None

    async def fetch_nitro_packs(self) -> Optional[List["StickerPack"]]:
        """
        List the sticker packs available to Nitro subscribers.

        Returns:
            A list of StickerPack objects if found, otherwise returns None

        """
        try:
            packs_data = await self.http.list_nitro_sticker_packs()
            return [StickerPack.from_dict(data, self) for data in packs_data]

        except NotFound:
            return None

    async def fetch_voice_regions(self) -> List["VoiceRegion"]:
        """
        List the voice regions available on Discord.

        Returns:
            A list of voice regions.

        """
        regions_data = await self.http.list_voice_regions()
        regions = VoiceRegion.from_list(regions_data)
        return regions

    async def connect_to_vc(
        self, guild_id: "Snowflake_Type", channel_id: "Snowflake_Type", muted: bool = False, deafened: bool = False
    ) -> ActiveVoiceState:
        """
        Connect the bot to a voice channel.

        Args:
            guild_id: id of the guild the voice channel is in.
            channel_id: id of the voice channel client wants to join.
            muted: Whether the bot should be muted when connected.
            deafened: Whether the bot should be deafened when connected.

        Returns:
            The new active voice state on successfully connection.

        """
        return await self._connection_state.voice_connect(guild_id, channel_id, muted, deafened)

    def get_bot_voice_state(self, guild_id: "Snowflake_Type") -> Optional[ActiveVoiceState]:
        """
        Get the bot's voice state for a guild.

        Args:
            guild_id: The target guild's id.

        Returns:
            The bot's voice state for the guild if connected, otherwise None.

        """
        return self._connection_state.get_voice_state(guild_id)

    async def change_presence(
        self, status: Optional[Union[str, Status]] = Status.ONLINE, activity: Optional[Union[Activity, str]] = None
    ) -> None:
        """
        Change the bots presence.

        Args:
            status: The status for the bot to be. i.e. online, afk, etc.
            activity: The activity for the bot to be displayed as doing.

        !!! note
            Bots may only be `playing` `streaming` `listening` `watching` or `competing`, other activity types are likely to fail.

        """
        await self._connection_state.change_presence(status, activity)
