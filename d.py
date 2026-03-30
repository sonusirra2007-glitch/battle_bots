from __future__ import annotations

"""
Cambridge Battlecode bot — v3.

Major improvements over v2:

COMMUNICATION (NEW):
  - Marker system: scouts encode enemy core position as a 32-bit integer and
    write it to a dedicated 'relay tile' near our core. All newly spawned bots
    read this before moving. Trail markers along the road network spread info
    across the map. Once ANY scout finds the enemy core, all subsequent scouts
    head directly there — no wasted movement to the wrong mirrored position.

ASSAULT (IMPROVED):
  - Self-heal: assault bots heal when HP ≤ 20 to sustain on enemy core.
  - Ally-heal: if a friendly bot on the same tile is damaged, a bot heals them.
  - Barrier shield: assault bots build cheap barriers between themselves and
    enemy turrets to absorb incoming shots.
  - Multiple scouts spread across different enemy core tiles (9-tile footprint)
    to maximise simultaneous fire damage (2 dmg/Ti, every round).

LAUNCHER (NEW):
  - The fortifier bot builds a launcher as its very first action.
  - The launcher flings adjacent scouts toward the furthest reachable walkable
    tile in the direction of the enemy core — slashing travel time significantly.
  - Multiple launchers can be built as the game progresses.

DOUBLE HARVEST (NEW):
  - Feeder bots mine a SECOND ore deposit once the first conveyor lane is
    connected. This doubles titanium income per lane. The second ore is mined
    in the same direction, extending the conveyor chain.

AXIONITE PIPELINE (IMPROVED):
  - Simplified and more reliable: axionite harvester → conveyor → foundry.
    Foundry outputs refined axionite directly via bridge to core.
    The bridge target is the core centre tile (always within distance² ≤ 9).

SCALE AWARENESS (NEW):
  - Checks ct.get_scale_percent() before expensive builds. Above 300% scale
    we severely throttle new buildings to avoid runaway cost inflation.

SENTINEL TURRETS (NEW):
  - Core builds a sentinel facing the enemy early once the primary lanes are
    stable. Sentinels (r²=32) provide long-range fire support; with refined
    axionite ammo they stun enemy scouts (+2 cooldown).

SELF-DESTRUCT (NEW):
  - Scouts that are stuck for 20+ consecutive turns self-destruct. This removes
    their cost-scale contribution (+20%) and frees up the 50-unit cap.
"""

from cambc import (
    Controller,
    Direction,
    EntityType,
    Environment,
    GameConstants,
    Position,
    Team,
)

# ---------------------------------------------------------------------------
# Direction / geometry helpers
# ---------------------------------------------------------------------------

DIRECTIONS = [d for d in Direction if d != Direction.CENTRE]
CARDINAL_DIRECTIONS = [
    Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST,
]
WALKABLE_BUILDINGS = {
    EntityType.CONVEYOR, EntityType.BRIDGE,
    EntityType.ARMOURED_CONVEYOR, EntityType.ROAD,
}
ROUTABLE_ENDPOINTS = {EntityType.CONVEYOR, EntityType.FOUNDRY}

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------
TARGET_FOUNDRIES        = 1    # 1 foundry by default (+100% scale is brutal)
LARGE_MAP_FOUNDRIES     = 2    # 2 on large maps
OFFENSE_MIN_ROUND       = 50
DEFENSE_MIN_ROUND       = 100
EXTRA_SPAWN_SURPLUS     = 45
EXTRA_SPAWN_COOLDOWN    = 4
TI_ONLY_OFFENSE_SURPLUS = 100
PRE_REFINERY_ASSAULT_CAP = 12
SCOUT_HEAL_HP           = 20   # heal when HP at or below this
SCOUT_SELF_DESTRUCT_STALL = 22 # turns stuck before self-destruct

# Marker encoding: enemy core position as (x<<8)|y (max coord 255, fine for ≤50 maps)
RELAY_DX, RELAY_DY = 2, 2     # relay marker offset from core centre


def encode_pos(pos: Position) -> int:
    return ((pos.x & 0xFF) << 8) | (pos.y & 0xFF)


def decode_pos(value: int) -> Position:
    return Position((value >> 8) & 0xFF, value & 0xFF)


def in_bounds(pos: Position, w: int, h: int) -> bool:
    return 0 <= pos.x < w and 0 <= pos.y < h


def step(pos: Position, d: Direction, n: int = 1) -> Position:
    for _ in range(n):
        pos = pos.add(d)
    return pos


def on_core_tile(pos: Position, core: Position) -> bool:
    return abs(pos.x - core.x) <= 1 and abs(pos.y - core.y) <= 1


def unique_dirs(primary: Direction) -> list[Direction]:
    if primary == Direction.CENTRE:
        primary = Direction.EAST
    ordered: list[Direction] = []

    def add(d: Direction) -> None:
        if d != Direction.CENTRE and d not in ordered:
            ordered.append(d)

    add(primary)
    left = right = primary
    for _ in range(3):
        left  = left.rotate_left()
        right = right.rotate_right()
        add(left); add(right)
    add(primary.opposite())
    for d in DIRECTIONS:
        add(d)
    return ordered[:8]


def cardinal_left(d: Direction) -> Direction:
    return {Direction.NORTH: Direction.WEST, Direction.EAST: Direction.NORTH,
            Direction.SOUTH: Direction.EAST, Direction.WEST: Direction.SOUTH}.get(d, d)


def cardinal_right(d: Direction) -> Direction:
    return {Direction.NORTH: Direction.EAST, Direction.EAST: Direction.SOUTH,
            Direction.SOUTH: Direction.WEST, Direction.WEST: Direction.NORTH}.get(d, d)


def cardinal_opposite(d: Direction) -> Direction:
    return {Direction.NORTH: Direction.SOUTH, Direction.EAST: Direction.WEST,
            Direction.SOUTH: Direction.NORTH, Direction.WEST: Direction.EAST}.get(d, d)


def directional_preferences(
    primary: Direction, secondary: Direction | None = None
) -> list[Direction]:
    ordered: list[Direction] = []

    def add(d: Direction) -> None:
        if d != Direction.CENTRE and d not in ordered:
            ordered.append(d)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        add(base)
        left = right = base
        for _ in range(3):
            left  = left.rotate_left()
            right = right.rotate_right()
            add(left); add(right)
    for d in DIRECTIONS:
        add(d)
    return ordered


def cardinal_directional_preferences(
    primary: Direction, secondary: Direction | None = None
) -> list[Direction]:
    ordered: list[Direction] = []
    diag = {
        Direction.NORTHEAST: (Direction.NORTH, Direction.EAST),
        Direction.SOUTHEAST: (Direction.SOUTH, Direction.EAST),
        Direction.SOUTHWEST: (Direction.SOUTH, Direction.WEST),
        Direction.NORTHWEST: (Direction.NORTH, Direction.WEST),
    }

    def add(d: Direction) -> None:
        if d in CARDINAL_DIRECTIONS and d not in ordered:
            ordered.append(d)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        if base in CARDINAL_DIRECTIONS:
            add(base); add(cardinal_left(base)); add(cardinal_right(base))
        else:
            for d in diag.get(base, ()):
                add(d)
    for d in CARDINAL_DIRECTIONS:
        add(d)
    return ordered


def slot_for(core: Position, d: Direction) -> Position:
    return step(core, d, 2)


def slot_target_for(core: Position, d: Direction) -> Position:
    return step(core, d, 1)


def ring_positions(core: Position, radius: int, w: int, h: int) -> list[Position]:
    out = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if max(abs(dx), abs(dy)) != radius:
                continue
            pos = Position(core.x + dx, core.y + dy)
            if in_bounds(pos, w, h):
                out.append(pos)
    return out


def core_tiles(core: Position, w: int, h: int) -> list[Position]:
    out = []
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            pos = Position(core.x + dx, core.y + dy)
            if in_bounds(pos, w, h):
                out.append(pos)
    return out


def nearby_core_id(ct: Controller) -> int | None:
    for eid in ct.get_nearby_buildings():
        if (ct.get_entity_type(eid) == EntityType.CORE
                and ct.get_team(eid) == ct.get_team()):
            return eid
    return None


def building_type_at(ct: Controller, pos: Position) -> EntityType | None:
    if not ct.is_in_vision(pos):
        return None
    bid = ct.get_tile_building_id(pos)
    return ct.get_entity_type(bid) if bid is not None else None


def direction_rank(preferred: Direction, candidate: Direction) -> int:
    if preferred == Direction.CENTRE:
        return 0
    prefs = directional_preferences(preferred)
    try:
        return prefs.index(candidate)
    except ValueError:
        return len(prefs)


def can_afford(ct: Controller, ti: int, ax: int = 0, reserve: int = 0) -> bool:
    titanium, axionite = ct.get_global_resources()
    return titanium - ti >= reserve and axionite >= ax


def scale_ok(ct: Controller, threshold: int = 300) -> bool:
    """Return False if cost scale is so high that new buildings are wasteful."""
    return ct.get_scale_percent() <= threshold


# ---------------------------------------------------------------------------
# Reserve helpers
# ---------------------------------------------------------------------------

def reserve_direct(ct: Controller) -> int:
    return ct.get_harvester_cost()[0] + ct.get_conveyor_cost()[0] * 5 + 25


def reserve_raw(ct: Controller) -> int:
    return (ct.get_foundry_cost()[0]
            + ct.get_harvester_cost()[0]
            + ct.get_conveyor_cost()[0] * 7
            + 50)


def reserve_defense(ct: Controller) -> int:
    return ct.get_gunner_cost()[0] * 2 + 40


def reserve_offense(ct: Controller) -> int:
    return ct.get_builder_bot_cost()[0] + 25


def titanium_low(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti < reserve_direct(ct)


def titanium_healthy_for_raw(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + 10


def titanium_ready_for_builders(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0]


def titanium_surplus_for_spawn(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + EXTRA_SPAWN_SURPLUS


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Player:
    def __init__(self):
        # --- common ---
        self.core_pos: Position | None = None
        self.core_id:  int | None      = None
        self.width:    int | None      = None
        self.height:   int | None      = None
        self.strategy_dirs: list[Direction] | None = None
        self.lane_dirs:     list[Direction] | None = None
        self.spawn_tiles:   list[Position]  | None = None

        # --- builder state ---
        self.initialized     = False
        self.role            = "unknown"
        self.home_dir:   Direction | None = None
        self.extra_dir:  Direction | None = None
        self.home_slot:  Position | None  = None
        self.home_target:Position | None  = None
        self.raw_slot:   Position | None  = None
        self.refined_slot:Position | None = None
        self.extra_slot: Position | None  = None
        self.extra_target:Position | None = None
        self.raw_dir:    Direction | None = None

        self.primary_titanium_done  = False
        self.second_ore_done        = False   # NEW: mined a 2nd ore in same lane
        self.raw_axionite_done      = False
        self.foundry_online         = False
        self.extra_titanium_done    = False
        self.refined_route_done     = False
        self.launcher_built         = False   # NEW: has this fortifier built a launcher?

        self.mission        = "idle"
        self.target_env:  Environment | None = None
        self.sink_pos:    Position | None    = None
        self.sink_target: Position | None    = None
        self.preferred_dir:Direction | None  = None
        self.target_ore:  Position | None    = None
        self.target_stand:Position | None    = None
        self.trail: list[Position] = []
        self.stall_turns  = 0
        self.total_stalls = 0   # NEW: cumulative stalls for self-destruct
        self.sweep_sign   = 1

        # --- scout / assault ---
        self.enemy_core_pos: Position | None = None
        self.scout_phase         = "advance"
        self.scout_advance_target: Position | None = None
        self.assault_mode        = False

        # --- core (spawner) ---
        self.spawned              = 0
        self.last_extra_spawn_rnd = -999

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

    def run(self, ct: Controller) -> None:
        if self.core_pos is None:
            self._bootstrap_common(ct)

        etype = ct.get_entity_type()
        if   etype == EntityType.CORE:        self.run_core(ct)
        elif etype == EntityType.BUILDER_BOT:  self.run_builder(ct)
        elif etype == EntityType.GUNNER:       self.run_gunner(ct)
        elif etype == EntityType.SENTINEL:     self.run_sentinel(ct)
        elif etype == EntityType.BREACH:       self.run_breach(ct)
        elif etype == EntityType.LAUNCHER:     self.run_launcher(ct)

    # -----------------------------------------------------------------------
    # Bootstrap
    # -----------------------------------------------------------------------

    def _bootstrap_common(self, ct: Controller) -> None:
        self.width  = ct.get_map_width()
        self.height = ct.get_map_height()
        if ct.get_entity_type() == EntityType.CORE:
            self.core_pos = ct.get_position()
            self.core_id  = ct.get_id()
        else:
            cid = nearby_core_id(ct)
            if cid is not None:
                self.core_id  = cid
                self.core_pos = ct.get_position(cid)
            else:
                self.core_pos = ct.get_position()

        assert self.width and self.height and self.core_pos
        centre  = Position(self.width // 2, self.height // 2)
        ordered = unique_dirs(self.core_pos.direction_to(centre))
        usable  = [d for d in ordered
                   if in_bounds(slot_for(self.core_pos, d), self.width, self.height)]
        for d in DIRECTIONS:
            if d not in usable and in_bounds(slot_for(self.core_pos, d), self.width, self.height):
                usable.append(d)
        self.strategy_dirs = usable[:8]

        lanes = [d for d in CARDINAL_DIRECTIONS
                 if in_bounds(slot_for(self.core_pos, d), self.width, self.height)]
        lanes.sort(key=lambda d: (
            slot_for(self.core_pos, d).distance_squared(centre),
            direction_rank(self.core_pos.direction_to(centre), d),
        ))
        if not lanes:
            lanes = [Direction.NORTH]
        while len(lanes) < 4:
            lanes.append(lanes[len(lanes) % len(lanes)])
        self.lane_dirs = lanes[:4]

        # Spawn tile pool
        role_tiles: list[Position] = []
        for d in self.lane_dirs[:3]:
            p = slot_target_for(self.core_pos, d)
            if p not in role_tiles:
                role_tiles.append(p)
        for d in self.strategy_dirs:
            p = slot_target_for(self.core_pos, d)
            if p not in role_tiles:
                role_tiles.append(p)
            if len(role_tiles) >= 5:
                break
        scout_tiles: list[Position] = []
        for d in DIRECTIONS:
            p = self.core_pos.add(d)
            if not in_bounds(p, self.width, self.height):
                continue
            if p in role_tiles or p in scout_tiles:
                continue
            if len(role_tiles) < 5:
                role_tiles.append(p)
            else:
                scout_tiles.append(p)
        self.spawn_tiles = role_tiles + scout_tiles

    # -----------------------------------------------------------------------
    # Marker communication helpers
    # -----------------------------------------------------------------------

    def _relay_tile(self) -> Position | None:
        """Well-known tile near our core where enemy position is broadcast."""
        if not self.core_pos or not self.width or not self.height:
            return None
        p = Position(self.core_pos.x + RELAY_DX, self.core_pos.y + RELAY_DY)
        if in_bounds(p, self.width, self.height):
            return p
        # Try opposite offset if OOB
        p2 = Position(self.core_pos.x - RELAY_DX, self.core_pos.y - RELAY_DY)
        if in_bounds(p2, self.width, self.height):
            return p2
        return None

    def _write_enemy_pos_marker(self, ct: Controller) -> None:
        """Write enemy core position to relay tile (non-blocking, free action)."""
        if not self.enemy_core_pos:
            return
        relay = self._relay_tile()
        if relay and ct.is_in_vision(relay) and ct.can_place_marker(relay):
            ct.place_marker(relay, encode_pos(self.enemy_core_pos))

    def _read_markers_for_enemy_pos(self, ct: Controller) -> None:
        """Scan visible markers for encoded enemy core position."""
        if self.enemy_core_pos is not None:
            return
        for pos in ct.get_nearby_tiles():
            bid = ct.get_tile_building_id(pos)
            if bid is None:
                continue
            if ct.get_entity_type(bid) != EntityType.MARKER:
                continue
            if ct.get_team(bid) != ct.get_team():
                continue
            val = ct.get_marker_value(bid)
            if val == 0:
                continue
            candidate = decode_pos(val)
            if (self.width and self.height
                    and in_bounds(candidate, self.width, self.height)):
                self.enemy_core_pos = candidate
                self.scout_phase = "attack"
                return

    def _place_trail_marker(self, ct: Controller) -> None:
        """Leave a trail marker at current position so others can follow."""
        if not self.enemy_core_pos:
            return
        current = ct.get_position()
        if ct.can_place_marker(current):
            ct.place_marker(current, encode_pos(self.enemy_core_pos))

    # -----------------------------------------------------------------------
    # Enemy scanning
    # -----------------------------------------------------------------------

    def _scan_enemy_core(self, ct: Controller) -> None:
        if self.enemy_core_pos is not None:
            return
        for eid in ct.get_nearby_buildings():
            if (ct.get_entity_type(eid) == EntityType.CORE
                    and ct.get_team(eid) != ct.get_team()):
                self.enemy_core_pos = ct.get_position(eid)
                self.scout_phase = "attack"
                # Broadcast immediately
                self._write_enemy_pos_marker(ct)
                return

    def _enemy_scored_targets(self, ct: Controller) -> list[Position]:
        prio = {
            EntityType.CORE: 5000, EntityType.BREACH: 800, EntityType.GUNNER: 700,
            EntityType.LAUNCHER: 650, EntityType.SENTINEL: 600,
            EntityType.BUILDER_BOT: 520, EntityType.FOUNDRY: 320,
            EntityType.HARVESTER: 260, EntityType.BARRIER: 180,
            EntityType.BRIDGE: 120, EntityType.CONVEYOR: 80,
            EntityType.SPLITTER: 80, EntityType.ARMOURED_CONVEYOR: 80,
            EntityType.ROAD: 30, EntityType.MARKER: 10,
        }
        scored: dict[Position, int] = {}
        for eid in ct.get_nearby_entities():
            if ct.get_team(eid) == ct.get_team():
                continue
            pos = ct.get_position(eid)
            etype = ct.get_entity_type(eid)
            hp = ct.get_hp(eid)
            max_hp = max(1, ct.get_max_hp(eid))
            score = prio.get(etype, 0) + (max_hp - hp) * 3
            if self.enemy_core_pos:
                score -= pos.distance_squared(self.enemy_core_pos)
            scored[pos] = scored.get(pos, 0) + score
        return [p for p, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0].x, kv[0].y))]

    def _preferred_core_targets(self, ct: Controller) -> list[Position]:
        if not self.enemy_core_pos or not self.width or not self.height:
            return []
        current = ct.get_position()
        return sorted(
            core_tiles(self.enemy_core_pos, self.width, self.height),
            key=lambda p: (current.distance_squared(p),
                           p.distance_squared(self.enemy_core_pos)),
        )

    # -----------------------------------------------------------------------
    # Turrets
    # -----------------------------------------------------------------------

    def run_gunner(self, ct: Controller) -> None:
        self._scan_enemy_core(ct)
        self._write_enemy_pos_marker(ct)
        for pos in self._preferred_core_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos); return
        for pos in self._enemy_scored_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos); return

    def run_sentinel(self, ct: Controller) -> None:
        self._scan_enemy_core(ct)
        self._write_enemy_pos_marker(ct)
        for pos in self._preferred_core_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos); return
        for pos in self._enemy_scored_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos); return

    def run_breach(self, ct: Controller) -> None:
        self._scan_enemy_core(ct)
        self._write_enemy_pos_marker(ct)
        for pos in self._preferred_core_targets(ct):
            if self._breach_safe(ct, pos) and ct.can_fire(pos):
                ct.fire(pos); return
        for pos in self._enemy_scored_targets(ct):
            if self._breach_safe(ct, pos) and ct.can_fire(pos):
                ct.fire(pos); return

    def _breach_safe(self, ct: Controller, target: Position) -> bool:
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dx == 0 and dy == 0:
                    continue
                check = Position(target.x + dx, target.y + dy)
                if not ct.is_in_vision(check):
                    continue
                bid = ct.get_tile_builder_bot_id(check)
                if bid and ct.get_team(bid) == ct.get_team():
                    return False
                bld = ct.get_tile_building_id(check)
                if bld and ct.get_team(bld) == ct.get_team():
                    return False
        return True

    def run_launcher(self, ct: Controller) -> None:
        """
        Launcher: fling adjacent friendly scouts toward the enemy core.
        Prefer the furthest reachable walkable tile in the enemy direction.
        """
        self._scan_enemy_core(ct)
        self._write_enemy_pos_marker(ct)
        current = ct.get_position()

        # Collect adjacent scouts
        adjacent_bots: list[Position] = []
        for d in DIRECTIONS:
            pos = current.add(d)
            if not ct.is_in_vision(pos):
                continue
            bid = ct.get_tile_builder_bot_id(pos)
            if bid and ct.get_team(bid) == ct.get_team():
                adjacent_bots.append(pos)
        if not adjacent_bots:
            return

        # Determine destination: closest passable tile toward enemy
        if self.enemy_core_pos:
            dest_dir = current.direction_to(self.enemy_core_pos)
        elif self.core_pos and self.width and self.height:
            mirror = Position(self.width - 1 - self.core_pos.x,
                              self.height - 1 - self.core_pos.y)
            dest_dir = current.direction_to(mirror)
        else:
            dest_dir = Direction.EAST

        # Collect passable tiles sorted nearest to furthest in enemy direction
        vision = ct.get_vision_radius_sq()
        candidates: list[Position] = []
        for pos in ct.get_nearby_tiles(vision):
            if not ct.is_tile_passable(pos):
                continue
            if ct.get_tile_builder_bot_id(pos) is not None:
                continue
            candidates.append(pos)

        # Score: prefer tiles far from us AND close to enemy core
        if self.enemy_core_pos:
            candidates.sort(key=lambda p: (
                p.distance_squared(self.enemy_core_pos),
                -current.distance_squared(p),
            ))
        else:
            candidates.sort(key=lambda p: -current.distance_squared(p))

        for target in candidates:
            for bot_pos in adjacent_bots:
                if ct.can_launch(bot_pos, target):
                    ct.launch(bot_pos, target)
                    return

    # -----------------------------------------------------------------------
    # Core
    # -----------------------------------------------------------------------

    def _target_foundry_count(self) -> int:
        if not self.width or not self.height:
            return TARGET_FOUNDRIES
        big = max(self.width, self.height) >= 30 or self.width * self.height >= 900
        n = LARGE_MAP_FOUNDRIES if big else TARGET_FOUNDRIES
        return min(n, len(self.lane_dirs or []), 3)

    def _feeder_index(self) -> int | None:
        if not self.role.startswith("feeder_"):
            return None
        try:
            return int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return None

    def _foundry_lane_indices(self, ct: Controller) -> list[int]:
        assert self.core_pos and self.lane_dirs
        preferred, fallback = [], []
        for i, d in enumerate(self.lane_dirs[:3]):
            pos = slot_for(self.core_pos, d)
            if not ct.is_in_vision(pos):
                fallback.append(i)
            elif ct.get_tile_env(pos) == Environment.EMPTY:
                preferred.append(i)
            else:
                fallback.append(i)
        return (preferred + fallback)[:self._target_foundry_count()]

    def _foundry_lane_dirs(self, ct: Controller) -> list[Direction]:
        assert self.lane_dirs
        return [self.lane_dirs[i] for i in self._foundry_lane_indices(ct)]

    def _feeder_should_build_foundry(self, ct: Controller) -> bool:
        idx = self._feeder_index()
        return idx is not None and idx in self._foundry_lane_indices(ct)

    def _refinery_started(self, ct: Controller) -> bool:
        _, ax = ct.get_global_resources()
        return ax > 0 or self._count_foundries(ct) > 0

    def _offense_ready(self, ct: Controller) -> bool:
        ti, ax = ct.get_global_resources()
        if ct.get_current_round() < OFFENSE_MIN_ROUND:
            return False
        if ax > 0 or self._count_foundries(ct) > 0:
            return ti >= reserve_offense(ct) + 50
        return ti >= reserve_raw(ct) + TI_ONLY_OFFENSE_SURPLUS

    def _defense_ready(self, ct: Controller) -> bool:
        if not self._refinery_started(ct):
            return False
        ti, _ = ct.get_global_resources()
        return ct.get_current_round() >= DEFENSE_MIN_ROUND or ti >= reserve_defense(ct) + 50

    def _count_primary_online(self, ct: Controller) -> int:
        assert self.core_pos and self.lane_dirs
        return sum(
            1 for d in self.lane_dirs[:3]
            if building_type_at(ct, slot_for(self.core_pos, d)) in ROUTABLE_ENDPOINTS
        )

    def _count_foundries(self, ct: Controller) -> int:
        assert self.core_pos and self.lane_dirs
        return sum(
            1 for d in self.lane_dirs[:3]
            if building_type_at(ct, slot_for(self.core_pos, d)) == EntityType.FOUNDRY
        )

    def _missing_primary_idx(self, ct: Controller) -> int | None:
        assert self.core_pos and self.lane_dirs
        for i, d in enumerate(self.lane_dirs[:3]):
            if building_type_at(ct, slot_for(self.core_pos, d)) not in ROUTABLE_ENDPOINTS:
                return i
        return None

    def _reserved_foundry_slots(self, ct: Controller) -> set[Position]:
        assert self.core_pos and self.lane_dirs
        return {slot_for(self.core_pos, d) for d in self._foundry_lane_dirs(ct)}

    def _maybe_vacate_reserved(self, ct: Controller) -> bool:
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        reserved = self._reserved_foundry_slots(ct)
        if current not in reserved or current == self.home_slot:
            return False
        for d in directional_preferences(current.direction_to(self.core_pos)):
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if dest in reserved and dest != self.home_slot:
                continue
            if ct.can_move(d):
                ct.move(d); return True
        return False

    def _should_join_assault(self, ct: Controller) -> bool:
        if not self._offense_ready(ct):
            return False
        rnd = ct.get_current_round()
        fnd = self._count_foundries(ct)
        if self.role == "scout":
            return True
        if self.role == "expander":
            return rnd >= 100
        if self.role == "fortifier":
            return rnd >= 150 and fnd >= 1
        if self.role.startswith("feeder"):
            if self._feeder_should_build_foundry(ct):
                return False
            return rnd >= 170 and fnd >= 1 and self.primary_titanium_done
        return False

    def run_core(self, ct: Controller) -> None:
        assert self.core_pos and self.strategy_dirs

        # Always scan + broadcast
        self._scan_enemy_core(ct)
        self._write_enemy_pos_marker(ct)

        rnd = ct.get_current_round()
        ti, _ = ct.get_global_resources()
        primary_online = self._count_primary_online(ct)
        foundries_online = self._count_foundries(ct)

        # --- Phase 1: spawn first 3 feeders unconditionally ---
        if self.spawned < 3:
            self._try_spawn_builder(ct, self.spawned)
            return

        # --- Phase 2: builders 4 & 5 once economy is stable ---
        if self.spawned < 5:
            late_fallback = rnd >= 70 and titanium_ready_for_builders(ct)
            refinery_ready = (
                self._refinery_started(ct)
                or rnd >= 150
                or ti >= reserve_raw(ct) + TI_ONLY_OFFENSE_SURPLUS
            )
            if (titanium_ready_for_builders(ct)
                    and (primary_online >= 3 or late_fallback)
                    and refinery_ready):
                self._try_spawn_builder(ct, self.spawned)
            return

        # --- Recovery: replace a dead feeder ---
        recovery = self._missing_primary_idx(ct)
        if recovery is not None and rnd >= 60 and titanium_ready_for_builders(ct):
            self._try_spawn_builder(ct, recovery)
            return

        # --- Continuous scout spawning ---
        pre_ref_extra = (
            not self._refinery_started(ct)
            and self.spawned < PRE_REFINERY_ASSAULT_CAP
            and rnd >= 80
            and ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + TI_ONLY_OFFENSE_SURPLUS
            and rnd - self.last_extra_spawn_rnd >= EXTRA_SPAWN_COOLDOWN * 2
        )
        post_ref_extra = (
            self._refinery_started(ct)
            and rnd >= 70
            and titanium_surplus_for_spawn(ct)
            and rnd - self.last_extra_spawn_rnd >= EXTRA_SPAWN_COOLDOWN
        )
        assault_swarm = (
            self._offense_ready(ct)
            and ct.get_unit_count() < 42
            and ti >= reserve_offense(ct) + ct.get_builder_bot_cost()[0] + 30
            and rnd - self.last_extra_spawn_rnd >= max(2, EXTRA_SPAWN_COOLDOWN // 2)
        )
        if primary_online >= 3 and (pre_ref_extra or post_ref_extra or assault_swarm):
            before = self.spawned
            self._try_spawn_builder(ct, max(self.spawned, 5))
            if self.spawned > before:
                self.last_extra_spawn_rnd = rnd
                return

        # --- Core builds structures ---
        if primary_online >= 3 and (
            foundries_online >= self._target_foundry_count() or self._defense_ready(ct)
        ):
            self._core_build_structures(ct)

    def _core_build_structures(self, ct: Controller) -> None:
        """Core places turrets/sentinels in ring-2 and ring-3."""
        assert self.core_pos and self.lane_dirs
        if not self.width or not self.height:
            return
        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
        for d in self._foundry_lane_dirs(ct):
            rs = self._choose_raw_slot(d)
            reserved.add(rs)
            ref = self._choose_refined_slot(d, rs)
            if ref:
                reserved.add(ref)
        ti, ax = ct.get_global_resources()

        for radius in (2, 3):
            for pos in ring_positions(self.core_pos, radius, self.width, self.height):
                if pos in reserved:
                    continue
                etype = building_type_at(ct, pos)
                if etype in (EntityType.BARRIER, EntityType.GUNNER,
                             EntityType.SENTINEL, EntityType.BREACH,
                             EntityType.LAUNCHER):
                    continue
                face = self.core_pos.direction_to(pos)
                if radius == 2:
                    # Prefer sentinel for long-range coverage (1 per core side)
                    if self._count_type_in_ring(ct, EntityType.SENTINEL, 2) < 2:
                        cost = ct.get_sentinel_cost()
                        if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and scale_ok(ct, 350):
                            if ct.can_build_sentinel(pos, face):
                                ct.build_sentinel(pos, face); return
                    # Breach if axionite available
                    if ax >= 10:
                        cost = ct.get_breach_cost()
                        if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and scale_ok(ct, 300):
                            if ct.can_build_breach(pos, face):
                                ct.build_breach(pos, face); return
                    cost = ct.get_gunner_cost()
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and scale_ok(ct, 400):
                        if ct.can_build_gunner(pos, face):
                            ct.build_gunner(pos, face); return
                else:
                    cost = ct.get_barrier_cost()
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                        if ct.can_build_barrier(pos):
                            ct.build_barrier(pos); return

    def _count_type_in_ring(self, ct: Controller, etype: EntityType, radius: int) -> int:
        if not self.core_pos or not self.width or not self.height:
            return 0
        return sum(
            1 for p in ring_positions(self.core_pos, radius, self.width, self.height)
            if building_type_at(ct, p) == etype
        )

    def _try_spawn_builder(self, ct: Controller, spawn_index: int) -> None:
        assert self.core_pos and self.spawn_tiles
        cost = ct.get_builder_bot_cost()
        reserve = reserve_direct(ct)
        if spawn_index < 3:
            reserve = max(80, reserve // 2)
        if not can_afford(ct, cost[0], cost[1], reserve=reserve):
            return

        if spawn_index < min(5, len(self.spawn_tiles)):
            pos = self.spawn_tiles[spawn_index]
            if ct.can_spawn(pos):
                ct.spawn_builder(pos)
                self.spawned += 1
            return

        scout_pool = self.spawn_tiles[5:] if len(self.spawn_tiles) > 5 else self.spawn_tiles[3:]
        if not scout_pool:
            return
        start = max(0, spawn_index - 5) % len(scout_pool)
        for offset in range(len(scout_pool)):
            pos = scout_pool[(start + offset) % len(scout_pool)]
            if ct.can_spawn(pos):
                ct.spawn_builder(pos)
                self.spawned += 1
                return

    # -----------------------------------------------------------------------
    # Builder
    # -----------------------------------------------------------------------

    def run_builder(self, ct: Controller) -> None:
        self._bootstrap_builder(ct)

        # Always scan for enemy + read/write markers
        self._scan_enemy_core(ct)
        self._read_markers_for_enemy_pos(ct)
        if self.enemy_core_pos:
            self._write_enemy_pos_marker(ct)

        if self._maybe_vacate_reserved(ct):
            return

        if self._should_join_assault(ct):
            self.assault_mode = True
            self._run_assault(ct)
            return

        if self.role == "scout":
            if self.assault_mode or self._offense_ready(ct):
                self.assault_mode = True
                self._run_assault(ct)
            else:
                self._run_support_builder(ct)
            return

        if self.role == "fortifier":
            # First action: build a launcher adjacent to core
            if not self.launcher_built:
                if self._try_build_launcher(ct):
                    self.launcher_built = True
                    return
                # Check if launcher already exists nearby
                for eid in ct.get_nearby_buildings():
                    if (ct.get_entity_type(eid) == EntityType.LAUNCHER
                            and ct.get_team(eid) == ct.get_team()):
                        self.launcher_built = True
                        break
                if not self.launcher_built:
                    # Move toward a good build position and wait
                    self._move_toward_launcher_pos(ct)
                    return

            if self._handle_defense(ct, barriers_first=True):
                return
            if not self.extra_titanium_done and not titanium_low(ct):
                self._assign_extra_titanium_mission()
            self._execute_mission(ct)
            return

        if self.mission == "idle":
            self._assign_next_mission(ct)

        if self.mission == "idle" and self._handle_opportunistic_turrets(ct):
            return

        self._execute_mission(ct)

    def _run_support_builder(self, ct: Controller) -> None:
        if self.mission == "idle":
            if not self.extra_titanium_done:
                self._assign_extra_titanium_mission()
        if self.mission == "idle" and self._handle_opportunistic_turrets(ct):
            return
        self._execute_mission(ct)

    def _try_build_launcher(self, ct: Controller) -> bool:
        """Try to build a launcher on any ring-2 tile within action radius."""
        assert self.core_pos and self.width and self.height
        cost = ct.get_launcher_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        for pos in ring_positions(self.core_pos, 2, self.width, self.height):
            if building_type_at(ct, pos) is not None:
                continue
            if ct.can_build_launcher(pos):
                ct.build_launcher(pos)
                return True
        return False

    def _move_toward_launcher_pos(self, ct: Controller) -> None:
        """Move onto a ring-1 tile so we can reach ring-2 launcher position."""
        if not self.core_pos or not self.width or not self.height:
            return
        current = ct.get_position()
        ring1 = ring_positions(self.core_pos, 1, self.width, self.height)
        if current in ring1:
            return  # already positioned
        target = min(ring1, key=lambda p: current.distance_squared(p))
        self._assault_step_toward(ct, target)

    def _bootstrap_builder(self, ct: Controller) -> None:
        if self.initialized:
            return
        if not self.core_pos:
            self._bootstrap_common(ct)
        assert self.core_pos and self.strategy_dirs and self.lane_dirs and self.spawn_tiles

        initial_pos = ct.get_position()

        if initial_pos in self.spawn_tiles[:3]:
            idx = self.spawn_tiles[:3].index(initial_pos)
            self.role      = f"feeder_{idx}"
            self.home_dir  = self.lane_dirs[idx]
            extra_cycle    = [3, 0, 1]
            self.extra_dir = self.lane_dirs[extra_cycle[idx] % len(self.lane_dirs)]
            self.sweep_sign = -1 if idx % 2 == 0 else 1
        elif len(self.spawn_tiles) > 3 and initial_pos == self.spawn_tiles[3]:
            self.role      = "fortifier"
            self.home_dir  = self.lane_dirs[min(3, len(self.lane_dirs) - 1)]
            self.extra_dir = self.lane_dirs[min(2, len(self.lane_dirs) - 1)]
            self.sweep_sign = 1
        elif len(self.spawn_tiles) > 4 and initial_pos == self.spawn_tiles[4]:
            self.role      = "expander"
            self.home_dir  = self.lane_dirs[min(3, len(self.lane_dirs) - 1)]
            self.extra_dir = self.lane_dirs[0]
            self.sweep_sign = 1
        else:
            self.role = "scout"
            assert self.width and self.height
            centre = Position(self.width // 2, self.height // 2)
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
            self.home_dir  = self.core_pos.direction_to(centre)
            self.extra_dir = self.home_dir
            self.sweep_sign = 1

        self.home_slot   = slot_for(self.core_pos, self.home_dir)
        self.home_target = slot_target_for(self.core_pos, self.home_dir)
        self.raw_slot    = self._choose_raw_slot(self.home_dir)
        self.refined_slot = self._choose_refined_slot(self.home_dir, self.raw_slot)
        self.extra_slot  = slot_for(self.core_pos, self.extra_dir)
        self.extra_target = slot_target_for(self.core_pos, self.extra_dir)

        if self.role.startswith("feeder") and self.raw_dir is None:
            self._init_raw_dir()

        self.initialized = True

    def _init_raw_dir(self) -> None:
        if not self.home_dir or not self.core_pos:
            self.raw_dir = self.home_dir; return
        assert self.width and self.height
        centre = Position(self.width // 2, self.height // 2)
        dx = centre.x - self.core_pos.x
        dy = centre.y - self.core_pos.y
        if abs(dx) >= abs(dy):
            primary = Direction.EAST if dx >= 0 else Direction.WEST
            second  = Direction.SOUTH if self.core_pos.y >= centre.y else Direction.NORTH
        else:
            primary = Direction.SOUTH if dy >= 0 else Direction.NORTH
            second  = Direction.EAST if dx > 0 else Direction.WEST
        third = cardinal_opposite(second)
        try:
            idx = int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            idx = 0
        self.raw_dir = [primary, second, third][idx % 3]

    def _raw_preferred_dir(self) -> Direction | None:
        return self.raw_dir or self.home_dir

    # -----------------------------------------------------------------------
    # ASSAULT — primary win condition
    # -----------------------------------------------------------------------

    def _run_assault(self, ct: Controller) -> None:
        """
        Walk onto enemy core tiles and fire() every round (2 damage/Ti/action).
        Heal when hurt. Build barrier shields against enemy turrets.
        Leave trail markers for other scouts.
        """
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        my_hp   = ct.get_hp()

        # Self-destruct if hopelessly stuck
        self.total_stalls += 1
        if self.total_stalls > SCOUT_SELF_DESTRUCT_STALL and self.enemy_core_pos is None:
            ct.self_destruct()
            return

        # --- Heal priority ---
        if my_hp <= SCOUT_HEAL_HP and ct.can_heal(current):
            ct.heal(current)
            self.total_stalls = 0
            return

        # --- Heal injured allies on same tile ---
        ally_needs_heal = False
        for d in list(DIRECTIONS) + [Direction.CENTRE]:
            pos = current.add(d) if d != Direction.CENTRE else current
            if not ct.is_in_vision(pos):
                continue
            bid = ct.get_tile_builder_bot_id(pos)
            if bid and ct.get_team(bid) == ct.get_team() and ct.get_hp(bid) <= SCOUT_HEAL_HP:
                ally_needs_heal = True
                break
        if ally_needs_heal and ct.can_heal(current):
            ct.heal(current)
            return

        # --- Scan / read markers ---
        self._scan_enemy_core(ct)
        self._read_markers_for_enemy_pos(ct)

        if self.enemy_core_pos is None:
            self._assault_advance(ct)
            return

        # --- Leave trail marker for following scouts ---
        self._place_trail_marker(ct)

        # --- ON enemy core tile: ATTACK ---
        if on_core_tile(current, self.enemy_core_pos):
            if ct.can_fire(current):
                ct.fire(current)
                self.total_stalls = 0
            # Even if on cooldown, stay on the tile
            return

        # --- Build a barrier shield against closest visible enemy turret ---
        if self._try_build_assault_barrier(ct):
            pass  # barrier built (doesn't cost action, so continue)

        # --- Navigate to a free enemy core tile ---
        core_tile_targets = sorted(
            core_tiles(self.enemy_core_pos, self.width, self.height),
            key=lambda p: current.distance_squared(p),
        )
        for tile in core_tile_targets:
            if self._assault_step_toward(ct, tile):
                self.total_stalls = 0
                return

        self.total_stalls += 1

    def _try_build_assault_barrier(self, ct: Controller) -> bool:
        """
        If there's an enemy turret adjacent-ish and an empty tile between us
        and it, build a barrier there to absorb shots.
        """
        if not self.enemy_core_pos:
            return False
        current = ct.get_position()
        # Find nearest visible enemy turret
        enemy_turret: Position | None = None
        nearest_dist = 10**9
        for eid in ct.get_nearby_entities():
            if ct.get_team(eid) == ct.get_team():
                continue
            etype = ct.get_entity_type(eid)
            if etype not in (EntityType.GUNNER, EntityType.SENTINEL, EntityType.BREACH):
                continue
            pos = ct.get_position(eid)
            d2 = current.distance_squared(pos)
            if d2 < nearest_dist:
                nearest_dist = d2
                enemy_turret = pos
        if enemy_turret is None or nearest_dist > 9:
            return False

        # Try to build a barrier between us and the turret
        mid_x = (current.x + enemy_turret.x) // 2
        mid_y = (current.y + enemy_turret.y) // 2
        barrier_pos = Position(mid_x, mid_y)
        if not (self.width and self.height and in_bounds(barrier_pos, self.width, self.height)):
            return False
        etype = building_type_at(ct, barrier_pos)
        if etype is not None:
            return False
        cost = ct.get_barrier_cost()
        if can_afford(ct, cost[0], cost[1], reserve=20):
            if ct.can_build_barrier(barrier_pos):
                ct.build_barrier(barrier_pos)
                return True
        return False

    def _assault_advance(self, ct: Controller) -> None:
        assert self.width and self.height and self.core_pos
        if self.scout_advance_target is None:
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
        self._assault_step_toward(ct, self.scout_advance_target)

    def _assault_step_toward(self, ct: Controller, target: Position) -> bool:
        """
        Greedy step toward target. Paves roads on empty tiles.
        Builder bots walk on: conveyors, roads, allied core (incl. enemy-owned).
        """
        assert self.width and self.height
        current = ct.get_position()
        if current == target:
            return False

        for d in directional_preferences(current.direction_to(target)):
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if ct.can_move(d):
                ct.move(d); return True
            env = ct.get_tile_env(dest)
            if env == Environment.EMPTY and ct.get_tile_building_id(dest) is None:
                cost = ct.get_road_cost()
                if can_afford(ct, cost[0], cost[1], reserve=15):
                    if ct.can_build_road(dest):
                        ct.build_road(dest)
                        if ct.can_move(d):
                            ct.move(d); return True
        return False

    # -----------------------------------------------------------------------
    # Mission assignment
    # -----------------------------------------------------------------------

    def _assign_next_mission(self, ct: Controller) -> None:
        if self.role.startswith("feeder"):
            self._assign_feeder_mission(ct)
        elif self.role == "expander":
            self._assign_expander_mission(ct)

    def _assign_feeder_mission(self, ct: Controller) -> None:
        # 1) Primary Ti lane
        if not self.primary_titanium_done:
            self._start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="primary_titanium",
            )
            return

        fnd_online = self._count_foundries(ct)
        fnd_target = self._target_foundry_count()
        should_foundry = self._feeder_should_build_foundry(ct)

        # 2) Raw axionite → foundry
        if (should_foundry and fnd_online < fnd_target
                and not self.raw_axionite_done
                and (titanium_healthy_for_raw(ct)
                     or (fnd_online == 0 and ct.get_current_round() >= 140))):
            self._start_mission(
                target_env=Environment.ORE_AXIONITE,
                sink_pos=self.raw_slot,
                sink_target=self.home_target,
                preferred_dir=self._raw_preferred_dir(),
                mission="raw_axionite",
            )
            return

        # 3) Wait for foundry
        if fnd_online < fnd_target:
            if not self.extra_titanium_done and (titanium_low(ct) or not should_foundry):
                self._assign_extra_titanium_mission()
            return

        # 4) Refined delivery
        home_fnd_online = (
            self.home_slot is not None
            and building_type_at(ct, self.home_slot) == EntityType.FOUNDRY
        )
        self.foundry_online = home_fnd_online
        if should_foundry and home_fnd_online and not self.refined_route_done:
            self._start_refined_delivery_mission()
            return

        # 5) SECOND ore deposit (NEW — double harvest)
        if not self.second_ore_done and self.extra_titanium_done:
            self._start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.extra_slot,
                sink_target=self.extra_target,
                preferred_dir=self.extra_dir,
                mission="second_ore",
            )
            return

        # 6) Defense
        if self._should_build_more_defense(ct):
            return

        # 7) Extra Ti
        if not self.extra_titanium_done and (
            titanium_low(ct) or not self._offense_ready(ct) or self._refinery_started(ct)
        ):
            self._assign_extra_titanium_mission()

    def _assign_expander_mission(self, ct: Controller) -> None:
        if not self.primary_titanium_done:
            self._start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="expansion_titanium",
            )
            return
        if self._count_foundries(ct) < self._target_foundry_count() and not self.extra_titanium_done:
            self._assign_extra_titanium_mission()
            return
        if not self.extra_titanium_done:
            self._assign_extra_titanium_mission()
            return
        # Second ore for expander too
        if not self.second_ore_done:
            self._start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.extra_slot,
                sink_target=self.extra_target,
                preferred_dir=self.extra_dir,
                mission="second_ore",
            )
            return
        self._should_build_more_defense(ct)

    def _assign_extra_titanium_mission(self) -> None:
        self._start_mission(
            target_env=Environment.ORE_TITANIUM,
            sink_pos=self.extra_slot,
            sink_target=self.extra_target,
            preferred_dir=self.extra_dir,
            mission="extra_titanium",
        )

    def _start_mission(
        self,
        target_env: Environment,
        sink_pos:   Position | None,
        sink_target:Position | None,
        preferred_dir: Direction | None,
        mission: str,
    ) -> None:
        if not sink_pos or not sink_target or not preferred_dir:
            return
        self.target_env   = target_env
        self.sink_pos     = sink_pos
        self.sink_target  = sink_target
        self.preferred_dir= preferred_dir
        self.target_ore   = None
        self.target_stand = None
        self.trail        = []
        self.stall_turns  = 0
        self.mission      = mission

    def _start_refined_delivery_mission(self) -> None:
        self.target_env = self.sink_pos = self.sink_target = self.preferred_dir = None
        self.target_ore = self.target_stand = None
        self.trail = []
        self.stall_turns = 0
        self.mission = "refined_delivery"

    # -----------------------------------------------------------------------
    # Mission execution
    # -----------------------------------------------------------------------

    def _execute_mission(self, ct: Controller) -> None:
        if self.mission == "idle":
            return
        if self.mission == "refined_delivery":
            self._execute_refined_delivery(ct)
            return
        if not self.sink_pos or not self.sink_target or not self.preferred_dir:
            self.mission = "idle"; return
        if self.mission.endswith("_connecting"):
            self._execute_connecting(ct)
        else:
            self._execute_searching(ct)

    def _execute_refined_delivery(self, ct: Controller) -> None:
        """
        Build a bridge next to the foundry that routes refined axionite to the
        core. Bridge target = core centre (within distance² ≤ 9 since foundry
        is 2 tiles away).
        """
        assert self.home_target and self.core_pos
        if self.refined_slot is None:
            self.refined_route_done = True; self.mission = "idle"; return

        # Move adjacent to the foundry (home_target is ring-1 toward foundry)
        current = ct.get_position()
        if current != self.home_target and not on_core_tile(current, self.core_pos):
            d = current.direction_to(self.home_target)
            if d != Direction.CENTRE and ct.can_move(d):
                ct.move(d)
            return

        # Build bridge at refined_slot pointing to core centre
        if self._ensure_bridge(ct, self.refined_slot, self.core_pos):
            self.refined_route_done = True
            self.mission = "idle"

    # -----------------------------------------------------------------------
    # Searching phase
    # -----------------------------------------------------------------------

    def _execute_searching(self, ct: Controller) -> None:
        assert self.sink_pos and self.sink_target and self.preferred_dir
        current = ct.get_position()

        if not self.trail:
            if current == self.sink_pos:
                self.trail = [current]
            else:
                self._move_to_sink(ct); return

        # Refresh ore target
        if self.target_ore is None or not self._ore_still_valid(ct, self.target_ore):
            self.target_ore, self.target_stand = self._plan_visible_ore(
                ct, self.target_env, self.preferred_dir
            )
        elif self.target_ore:
            self.target_stand = self._find_build_stand(ct, self.target_ore)

        if self.target_ore is not None:
            if current.distance_squared(self.target_ore) <= GameConstants.ACTION_RADIUS_SQ:
                if abs(current.x - self.target_ore.x) + abs(current.y - self.target_ore.y) != 1:
                    if not self._move_towards(ct, self.target_stand, self.preferred_dir):
                        self.stall_turns += 1
                        if self.stall_turns >= 3:
                            self.target_ore = self.target_stand = None
                            self.preferred_dir = cardinal_right(self.preferred_dir)
                            self.stall_turns = 0
                    else:
                        self.stall_turns = 0
                    return
                reserve = reserve_raw(ct) if self.mission == "raw_axionite" else reserve_direct(ct)
                hc = ct.get_harvester_cost()
                if can_afford(ct, hc[0], hc[1], reserve) and ct.can_build_harvester(self.target_ore):
                    ct.build_harvester(self.target_ore)
                    self.mission = f"{self.mission}_connecting"
                return

            mt = self.target_stand or self.target_ore
            if not self._move_towards(ct, mt, self.preferred_dir):
                self.stall_turns += 1
                if self.stall_turns >= 3:
                    self.target_ore = self.target_stand = None
                    self.preferred_dir = cardinal_right(self.preferred_dir)
                    self.stall_turns = 0
            else:
                self.stall_turns = 0
            return

        # Wander toward map centre for axionite
        if self.target_env == Environment.ORE_AXIONITE and self.width and self.height:
            centre = Position(self.width // 2, self.height // 2)
            if current.distance_squared(centre) > 8:
                if not self._move_towards(ct, centre, self.preferred_dir):
                    self.stall_turns += 1
                    if self.stall_turns >= 3:
                        self.preferred_dir = cardinal_right(self.preferred_dir)
                        self.stall_turns = 0
                else:
                    self.stall_turns = 0
                return

        if not self._move_towards(ct, None, self._swept_direction()):
            self.stall_turns += 1
            if self.stall_turns >= 3:
                self.preferred_dir = cardinal_right(self.preferred_dir)
                self.stall_turns = 0
        else:
            self.stall_turns = 0

    def _swept_direction(self) -> Direction:
        assert self.preferred_dir
        seg = (len(self.trail) // 6) % 4
        d = self.preferred_dir
        if seg == 1:
            return cardinal_right(d) if self.sweep_sign > 0 else cardinal_left(d)
        if seg == 2:
            return cardinal_opposite(d)
        return d

    # -----------------------------------------------------------------------
    # Connecting phase
    # -----------------------------------------------------------------------

    def _execute_connecting(self, ct: Controller) -> None:
        assert self.sink_target and self.target_env
        if not self.trail:
            self.mission = "idle"; return
        current = ct.get_position()
        if current not in self.trail:
            return

        ci = self.trail.index(current)
        if ci < len(self.trail) - 1:
            next_tile = self.trail[ci + 1]
            need_dir  = next_tile.direction_to(current)
            if not self._has_conveyor(ct, next_tile, need_dir):
                self._ensure_conveyor(ct, next_tile, need_dir)
                return

        if ci > 0:
            md = current.direction_to(self.trail[ci - 1])
            if ct.can_move(md):
                ct.move(md)
            return

        if self.mission == "raw_axionite_connecting":
            fc = ct.get_foundry_cost(); cc = ct.get_conveyor_cost()
            if not can_afford(ct, fc[0] + cc[0], 0, reserve=reserve_direct(ct)):
                return
            if not self.home_slot:
                return
            if not self._ensure_conveyor(ct, self.trail[0],
                                         self.trail[0].direction_to(self.home_slot)):
                return
            if ct.get_tile_builder_bot_id(self.home_slot):
                return
            if not self._ensure_foundry(ct, self.home_slot):
                return
            self.raw_axionite_done = True
            self.foundry_online    = True
            self.mission           = "idle"
            return

        self._ensure_conveyor(ct, self.trail[0],
                              self.trail[0].direction_to(self.sink_target))

        if self.mission in ("primary_titanium_connecting",
                            "expansion_titanium_connecting"):
            self.primary_titanium_done = True
        elif self.mission == "extra_titanium_connecting":
            self.extra_titanium_done = True
        elif self.mission == "second_ore_connecting":
            self.second_ore_done = True

        self.mission = "idle"

    # -----------------------------------------------------------------------
    # Building helpers
    # -----------------------------------------------------------------------

    def _has_conveyor(self, ct: Controller, pos: Position, d: Direction) -> bool:
        bid = ct.get_tile_building_id(pos)
        if bid is None:
            return False
        return (ct.get_entity_type(bid) == EntityType.CONVEYOR
                and ct.get_direction(bid) == d)

    def _ensure_conveyor(self, ct: Controller, pos: Position, d: Direction) -> bool:
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.CONVEYOR and ct.get_direction(bid) == d:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        res = reserve_raw(ct) if self.mission == "raw_axionite_connecting" else reserve_direct(ct)
        cost = ct.get_conveyor_cost()
        if not can_afford(ct, cost[0], cost[1], res):
            return False
        if ct.can_build_conveyor(pos, d):
            ct.build_conveyor(pos, d); return True
        return False

    def _ensure_foundry(self, ct: Controller, pos: Position | None) -> bool:
        if pos is None:
            return False
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.FOUNDRY:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_foundry_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_foundry(pos):
            ct.build_foundry(pos); return True
        return False

    def _ensure_bridge(
        self, ct: Controller, pos: Position | None, target: Position | None
    ) -> bool:
        if pos is None or target is None:
            return False
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.BRIDGE and ct.get_bridge_target(bid) == target:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_bridge_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_bridge(pos, target):
            ct.build_bridge(pos, target); return True
        return False

    # -----------------------------------------------------------------------
    # Movement
    # -----------------------------------------------------------------------

    def _move_to_sink(self, ct: Controller) -> None:
        assert self.sink_pos and self.preferred_dir and self.core_pos
        current = ct.get_position()
        if current == self.sink_pos:
            self.trail = [current]; return
        if ct.is_in_vision(self.sink_pos) and not ct.is_tile_passable(self.sink_pos):
            rc = ct.get_road_cost()
            if can_afford(ct, rc[0], rc[1], reserve=reserve_direct(ct)) and ct.can_build_road(self.sink_pos):
                ct.build_road(self.sink_pos)
        d = current.direction_to(self.sink_pos)
        if d != Direction.CENTRE and ct.can_move(d):
            ct.move(d)
            if ct.get_position() == self.sink_pos:
                self.trail = [self.sink_pos]

    def _move_towards(
        self, ct: Controller, target: Position | None, fallback: Direction
    ) -> bool:
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        if target is None:
            candidates = cardinal_directional_preferences(fallback)
        else:
            dx = target.x - current.x
            dy = target.y - current.y
            h = Direction.EAST if dx > 0 else Direction.WEST if dx < 0 else Direction.CENTRE
            v = Direction.SOUTH if dy > 0 else Direction.NORTH if dy < 0 else Direction.CENTRE
            if abs(dx) > abs(dy):
                primary = h; secondary = v if v != Direction.CENTRE else fallback
            elif abs(dy) > abs(dx):
                primary = v; secondary = h if h != Direction.CENTRE else fallback
            elif fallback in (h, v):
                primary = fallback; secondary = v if primary == h else h
            else:
                primary = h if h != Direction.CENTRE else v
                secondary = v if primary == h else h
            if primary == Direction.CENTRE:
                primary = fallback
            candidates = cardinal_directional_preferences(primary, secondary)

        recent = set(self.trail[-6:])
        for allow_recent in (False, True):
            for d in candidates:
                dest = current.add(d)
                if not in_bounds(dest, self.width, self.height):
                    continue
                if on_core_tile(dest, self.core_pos):
                    continue
                if not allow_recent and dest in recent:
                    continue
                if self._try_step(ct, d):
                    self._update_trail(ct.get_position())
                    return True
        return False

    def _try_step(self, ct: Controller, d: Direction) -> bool:
        dest = ct.get_position().add(d)
        if ct.can_move(d):
            ct.move(d); return True
        if ct.get_tile_env(dest) != Environment.EMPTY:
            return False
        if ct.get_tile_building_id(dest) is not None:
            return False
        cost = ct.get_road_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_road(dest):
            ct.build_road(dest)
            if ct.can_move(d):
                ct.move(d); return True
        return False

    def _update_trail(self, pos: Position) -> None:
        if not self.trail:
            self.trail = [pos]; return
        if pos in self.trail:
            self.trail = self.trail[: self.trail.index(pos) + 1]
        else:
            self.trail.append(pos)

    # -----------------------------------------------------------------------
    # Ore planning
    # -----------------------------------------------------------------------

    def _plan_visible_ore(
        self, ct: Controller, env: Environment, preferred: Direction
    ) -> tuple[Position | None, Position | None]:
        current = ct.get_position()
        best_ore = best_stand = None
        best_score = 10 ** 9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != env:
                continue
            if ct.get_tile_building_id(pos) is not None:
                continue
            stand = self._find_build_stand(ct, pos)
            if stand is None:
                continue
            score = (current.distance_squared(stand) * 10
                     + current.distance_squared(pos)
                     + direction_rank(preferred, current.direction_to(stand)))
            if score < best_score:
                best_ore, best_stand, best_score = pos, stand, score
        return best_ore, best_stand

    def _ore_still_valid(self, ct: Controller, pos: Position) -> bool:
        if not ct.is_in_vision(pos):
            return False
        if self.target_env and ct.get_tile_env(pos) != self.target_env:
            return False
        return ct.get_tile_building_id(pos) is None

    def _find_build_stand(self, ct: Controller, ore: Position | None) -> Position | None:
        if ore is None:
            return None
        current = ct.get_position()
        candidates: list[Position] = []
        for pos in [current, *ct.get_nearby_tiles()]:
            if abs(pos.x - ore.x) + abs(pos.y - ore.y) != 1:
                continue
            if ct.get_tile_env(pos) != Environment.EMPTY and not ct.is_tile_passable(pos):
                continue
            if ct.get_tile_building_id(pos) is not None and not ct.is_tile_passable(pos):
                continue
            candidates.append(pos)
        if not candidates:
            return None
        candidates.sort(key=lambda p: (
            current.distance_squared(p),
            direction_rank(self.preferred_dir or Direction.CENTRE,
                           current.direction_to(p)),
        ))
        return candidates[0]

    # -----------------------------------------------------------------------
    # Defense
    # -----------------------------------------------------------------------

    def _should_build_more_defense(self, ct: Controller) -> bool:
        return self._defense_ready(ct) and self._handle_defense(ct, barriers_first=False)

    def _handle_opportunistic_turrets(self, ct: Controller) -> bool:
        if not self._defense_ready(ct) or not self.core_pos:
            return False
        if not on_core_tile(ct.get_position(), self.core_pos):
            return False
        return self._handle_defense(ct, barriers_first=False)

    def _handle_defense(self, ct: Controller, barriers_first: bool) -> bool:
        assert self.core_pos
        if not on_core_tile(ct.get_position(), self.core_pos):
            d = ct.get_position().direction_to(self.core_pos)
            if d != Direction.CENTRE and ct.can_move(d):
                ct.move(d); return True

        barrier_pos, turret_pos = self._defense_positions()
        _, ax = ct.get_global_resources()

        if barriers_first:
            for pos in barrier_pos:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_barrier(pos):
                    ct.build_barrier(pos); return True

        for pos in turret_pos:
            if building_type_at(ct, pos) in (EntityType.GUNNER, EntityType.SENTINEL,
                                              EntityType.BREACH, EntityType.LAUNCHER):
                continue
            face = self.core_pos.direction_to(pos)
            # Sentinel first for range
            if self._count_type_in_ring(ct, EntityType.SENTINEL, 2) < 2:
                cost = ct.get_sentinel_cost()
                if (can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                        and scale_ok(ct, 350) and ct.can_build_sentinel(pos, face)):
                    ct.build_sentinel(pos, face); return True
            if ax >= 10:
                cost = ct.get_breach_cost()
                if (can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                        and scale_ok(ct, 300) and ct.can_build_breach(pos, face)):
                    ct.build_breach(pos, face); return True
            cost = ct.get_gunner_cost()
            if (can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                    and scale_ok(ct, 400) and ct.can_build_gunner(pos, face)):
                ct.build_gunner(pos, face); return True

        # Ring-3 extras
        if self.width and self.height:
            reserved = {slot_for(self.core_pos, d) for d in (self.lane_dirs or [])}
            for pos in ring_positions(self.core_pos, 3, self.width, self.height):
                if pos in reserved:
                    continue
                if building_type_at(ct, pos) in (EntityType.GUNNER, EntityType.BARRIER,
                                                  EntityType.SENTINEL, EntityType.BREACH):
                    continue
                cost = ct.get_gunner_cost()
                face = self.core_pos.direction_to(pos)
                if (can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                        and scale_ok(ct, 350) and ct.can_build_gunner(pos, face)):
                    ct.build_gunner(pos, face); return True

        if not barriers_first:
            for pos in barrier_pos:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_barrier(pos):
                    ct.build_barrier(pos); return True
        return False

    def _defense_positions(self) -> tuple[list[Position], list[Position]]:
        assert self.core_pos and self.lane_dirs and self.width and self.height
        ring2: list[Position] = []
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                if max(abs(dx), abs(dy)) != 2:
                    continue
                pos = Position(self.core_pos.x + dx, self.core_pos.y + dy)
                if in_bounds(pos, self.width, self.height):
                    ring2.append(pos)

        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
        reserved.update(self._choose_raw_slot(d) for d in self.lane_dirs[:3])
        for d in self.lane_dirs[:self._target_foundry_count()]:
            r = self._choose_refined_slot(d, self._choose_raw_slot(d))
            if r:
                reserved.add(r)

        available = [p for p in ring2 if p not in reserved]
        available.sort(key=lambda p: (
            abs(p.x - self.core_pos.x) + abs(p.y - self.core_pos.y), p.y, p.x
        ))
        split = max(0, len(available) // 4)
        return available[:split], available[split:]

    # -----------------------------------------------------------------------
    # Slot helpers
    # -----------------------------------------------------------------------

    def _choose_raw_slot(self, d: Direction) -> Position:
        assert self.core_pos and self.width and self.height
        foundry = slot_for(self.core_pos, d)
        centre  = Position(self.width // 2, self.height // 2)
        candidates = [foundry.add(cardinal_left(d)), foundry.add(cardinal_right(d))]
        home_slots = {slot_for(self.core_pos, ld) for ld in (self.lane_dirs or [])}
        best, best_score = candidates[0], 999
        for pos in candidates:
            score = 0
            if pos in home_slots:             score += 50
            if not in_bounds(pos, self.width, self.height): score += 100
            if on_core_tile(pos, self.core_pos):            score += 100
            if in_bounds(pos, self.width, self.height):
                score += pos.distance_squared(centre) // 4
            if score < best_score:
                best, best_score = pos, score
        return best

    def _choose_refined_slot(
        self, d: Direction, raw_slot: Position | None
    ) -> Position | None:
        assert self.core_pos and self.width and self.height
        foundry = slot_for(self.core_pos, d)
        candidates = [foundry.add(cardinal_left(d)), foundry.add(cardinal_right(d))]
        home_slots = {slot_for(self.core_pos, ld) for ld in (self.lane_dirs or [])}
        best: Position | None = None
        best_score = 999
        for pos in candidates:
            score = 0
            if raw_slot and pos == raw_slot: score += 200
            if pos in home_slots:            score += 50
            if not in_bounds(pos, self.width, self.height): score += 100
            if on_core_tile(pos, self.core_pos):            score += 100
            if score < best_score:
                best, best_score = pos, score
        return best if best_score < 100 else None