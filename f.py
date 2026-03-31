from __future__ import annotations

"""Pure-titanium Battlecode bot — improved mining chains and aggressive offense.

Economy:
  - Each miner bot walks to a titanium ore deposit, builds a harvester, then
    lays a cardinal conveyor chain tile-by-tile back to an adjacent core tile.
    The chain is explicit (no trail confusion): [ore_adj, ..., core_adj].
  - Multiple miners spread across different ore deposits automatically via a
    global "claimed" marker system (markers placed on ore tiles).
  - After delivering its first deposit, a miner searches for the next nearest
    unclaimed titanium and repeats.

Offense:
  - Assault bots rush toward the enemy core using a BFS flow field.
  - When adjacent to the enemy core they call fire(my_pos) to deal 2 chip
    damage per turn (costs 2 Ti each).
  - Assault bots actively destroy enemy conveyors and harvesters they pass
    to starve the enemy economy.
  - Siege gunners are placed on the four cardinal positions at distance 2 from
    the enemy core (direct line-of-sight), fed by the nearest Ti harvester via
    a conveyor chain built by a dedicated assault bot.
  - Launchers catapult builder bots forward to accelerate the assault.
"""

from collections import deque

from cambc import (
    Controller,
    Direction,
    EntityType,
    Environment,
    GameConstants,
    Position,
)

# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------

DIRECTIONS = [d for d in Direction if d != Direction.CENTRE]
CARDINAL_DIRECTIONS = [Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST]

WALKABLE_BUILDINGS = {
    EntityType.CONVEYOR,
    EntityType.ARMOURED_CONVEYOR,
    EntityType.ROAD,
    EntityType.BRIDGE,
}

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

OPENING_MINERS       = 3    # miners spawned before assault bots begin
TARGET_ASSAULT_BOTS  = 16   # total assault bots to maintain
OFFENSE_MIN_ROUND    = 70   # force offense after this round even if Ti is low
BASE_RESERVE         = 60   # never dip below this much Ti
ASSAULT_SURPLUS      = 100  # surplus above reserve needed before spawning assault
ASSAULT_COOLDOWN     = 6    # min rounds between assault spawns
STALL_LIMIT          = 4    # turns before a stuck bot paves a road or builds launcher
MAX_CHAIN_LEN        = 32   # BFS path length cap for conveyor chains

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def in_bounds(pos: Position, w: int, h: int) -> bool:
    return 0 <= pos.x < w and 0 <= pos.y < h

def on_core_tile(pos: Position, core: Position) -> bool:
    return abs(pos.x - core.x) <= 1 and abs(pos.y - core.y) <= 1

def step(pos: Position, d: Direction, n: int = 1) -> Position:
    for _ in range(n):
        pos = pos.add(d)
    return pos

def ring_positions(centre: Position, radius: int, w: int, h: int) -> list[Position]:
    result: list[Position] = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if max(abs(dx), abs(dy)) != radius:
                continue
            p = Position(centre.x + dx, centre.y + dy)
            if in_bounds(p, w, h):
                result.append(p)
    return result

def core_tiles(core: Position, w: int, h: int) -> list[Position]:
    return [
        Position(core.x + dx, core.y + dy)
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if in_bounds(Position(core.x + dx, core.y + dy), w, h)
    ]

def adjacent_cardinals(pos: Position) -> list[Position]:
    return [pos.add(d) for d in CARDINAL_DIRECTIONS]

def cardinal_left(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.WEST, Direction.EAST: Direction.NORTH,
        Direction.SOUTH: Direction.EAST, Direction.WEST: Direction.SOUTH,
    }.get(d, d)

def cardinal_right(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.EAST, Direction.EAST: Direction.SOUTH,
        Direction.SOUTH: Direction.WEST, Direction.WEST: Direction.NORTH,
    }.get(d, d)

def cardinal_opposite(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.SOUTH, Direction.EAST: Direction.WEST,
        Direction.SOUTH: Direction.NORTH, Direction.WEST: Direction.EAST,
    }.get(d, d)

def symmetry_guesses(core: Position, w: int, h: int) -> list[Position]:
    candidates = [
        Position(w - 1 - core.x, h - 1 - core.y),
        Position(w - 1 - core.x, core.y),
        Position(core.x, h - 1 - core.y),
    ]
    seen: list[Position] = []
    for g in candidates:
        if g != core and g not in seen:
            seen.append(g)
    return seen or [core]

# ---------------------------------------------------------------------------
# Direction preference lists
# ---------------------------------------------------------------------------

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
        l, r = base, base
        for _ in range(3):
            l = l.rotate_left()
            r = r.rotate_right()
            add(l)
            add(r)

    for d in DIRECTIONS:
        add(d)
    return ordered

def direction_rank(preferred: Direction, candidate: Direction) -> int:
    prefs = directional_preferences(preferred)
    try:
        return prefs.index(candidate)
    except ValueError:
        return len(prefs)

# ---------------------------------------------------------------------------
# Entity / building helpers
# ---------------------------------------------------------------------------

def nearby_core_id(ct: Controller) -> int | None:
    for eid in ct.get_nearby_buildings():
        if ct.get_entity_type(eid) == EntityType.CORE and ct.get_team(eid) == ct.get_team():
            return eid
    return None

def building_type_at(ct: Controller, pos: Position) -> EntityType | None:
    if not ct.is_in_vision(pos):
        return None
    bid = ct.get_tile_building_id(pos)
    return None if bid is None else ct.get_entity_type(bid)

def can_afford(ct: Controller, ti: int, ax: int = 0, reserve: int = 0) -> bool:
    t, a = ct.get_global_resources()
    return t - ti >= reserve and a >= ax

# ---------------------------------------------------------------------------
# Reserve helpers
# ---------------------------------------------------------------------------

def reserve_eco(ct: Controller) -> int:
    return ct.get_harvester_cost()[0] + ct.get_conveyor_cost()[0] * 6 + BASE_RESERVE

def reserve_assault(ct: Controller) -> int:
    return ct.get_gunner_cost()[0] + BASE_RESERVE

def surplus_for_assault(ct: Controller) -> bool:
    t, _ = ct.get_global_resources()
    return t >= reserve_eco(ct) + ct.get_builder_bot_cost()[0] + ASSAULT_SURPLUS

# ---------------------------------------------------------------------------
# BFS cardinal chain planner
# ---------------------------------------------------------------------------

def plan_chain(
    start: Position,
    goal: Position,
    w: int,
    h: int,
    passable_fn,  # Callable[[Position], bool]
) -> list[Position] | None:
    """
    BFS over cardinal moves from start to goal.
    Returns [start, ..., goal] or None.  Limited to MAX_CHAIN_LEN steps.
    """
    if start == goal:
        return [start]
    visited: set[tuple[int, int]] = {(start.x, start.y)}
    # Store (pos, path) — path is a list of positions
    queue: deque[tuple[Position, list[Position]]] = deque()
    queue.append((start, [start]))
    while queue:
        pos, path = queue.popleft()
        if len(path) >= MAX_CHAIN_LEN:
            continue
        for d in CARDINAL_DIRECTIONS:
            nxt = pos.add(d)
            k = (nxt.x, nxt.y)
            if k in visited or not in_bounds(nxt, w, h):
                continue
            if nxt == goal:
                return path + [goal]
            if not passable_fn(nxt):
                continue
            visited.add(k)
            queue.append((nxt, path + [nxt]))
    return None

# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Player:
    def __init__(self) -> None:
        # --- shared ---
        self.core_pos: Position | None = None
        self.core_id: int | None = None
        self.width: int | None = None
        self.height: int | None = None
        self.lane_dirs: list[Direction] | None = None
        self.enemy_guesses: list[Position] | None = None
        self.miner_spawn_tiles: list[Position] | None = None
        self.assault_spawn_tiles: list[Position] | None = None

        # --- builder ---
        self.initialized = False
        self.role = "unknown"
        self.initial_pos: Position | None = None
        self.lane_index: int | None = None
        self.home_dir: Direction | None = None
        self.home_slot: Position | None = None   # core-adjacent tile where chain ends
        self.preferred_dir: Direction | None = None

        # Miner state machine
        # States: find_ore → walk_to_stand → build_harvester → build_chain → (repeat)
        self.miner_state = "find_ore"
        self.target_ore: Position | None = None
        self.target_stand: Position | None = None
        self.conv_chain: list[Position] | None = None   # [ore_output, ..., home_slot]
        self.conv_idx = 0
        self.stall_turns = 0
        self.sweep_sign = 1

        # --- assault ---
        self.enemy_core_pos: Position | None = None
        self.assault_guess_index = 0
        self.assault_exit_dir: Direction | None = None
        self.flow_sig: tuple | None = None
        self.flow: dict[tuple[int, int], int] | None = None
        self.ammo_gunner_pos: Position | None = None
        self.ammo_chain: list[Position] | None = None
        self.ammo_chain_idx = 0

        # --- core (spawner) ---
        self.opening_spawned = 0
        self.assault_cursor = 0
        self.last_assault_round = -999

    # ===================================================================
    # Top-level dispatch
    # ===================================================================

    def run(self, ct: Controller) -> None:
        if self.core_pos is None:
            self._bootstrap(ct)

        etype = ct.get_entity_type()
        if etype == EntityType.CORE:
            self._run_core(ct)
        elif etype == EntityType.BUILDER_BOT:
            self._run_builder(ct)
        elif etype == EntityType.GUNNER:
            self._run_gunner(ct)
        elif etype == EntityType.LAUNCHER:
            self._run_launcher(ct)

    # ===================================================================
    # Bootstrap
    # ===================================================================

    def _bootstrap(self, ct: Controller) -> None:
        self.width = ct.get_map_width()
        self.height = ct.get_map_height()
        w, h = self.width, self.height

        if ct.get_entity_type() == EntityType.CORE:
            self.core_pos = ct.get_position()
            self.core_id = ct.get_id()
        else:
            cid = nearby_core_id(ct)
            self.core_id = cid
            self.core_pos = ct.get_position(cid) if cid is not None else ct.get_position()

        cp = self.core_pos
        centre = Position(w // 2, h // 2)

        # Cardinal lanes sorted by proximity to map centre (most open direction first)
        lanes = [
            d for d in CARDINAL_DIRECTIONS
            if in_bounds(step(cp, d, 2), w, h)
        ]
        lanes.sort(key=lambda d: step(cp, d, 2).distance_squared(centre))
        self.lane_dirs = lanes or [Direction.NORTH]

        self.enemy_guesses = symmetry_guesses(cp, w, h)

        # Miner spawn tiles: one tile in each cardinal lane direction
        miner_tiles = [step(cp, d, 1) for d in self.lane_dirs]
        used = set(miner_tiles)

        # Assault spawn tiles: remaining core-adjacent tiles
        assault_tiles = [
            Position(cp.x + dx, cp.y + dy)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if in_bounds(Position(cp.x + dx, cp.y + dy), w, h)
            and Position(cp.x + dx, cp.y + dy) not in used
        ] or miner_tiles

        self.miner_spawn_tiles = miner_tiles
        self.assault_spawn_tiles = assault_tiles

    # ===================================================================
    # Core / spawner
    # ===================================================================

    def _run_core(self, ct: Controller) -> None:
        assert self.miner_spawn_tiles and self.assault_spawn_tiles
        rnd = ct.get_current_round()

        # Phase 1: spawn opening miners
        if self.opening_spawned < OPENING_MINERS:
            idx = self.opening_spawned % len(self.miner_spawn_tiles)
            if self._spawn_at(ct, self.miner_spawn_tiles[idx], reserve=BASE_RESERVE):
                self.opening_spawned += 1
            return

        # Phase 2: spawn assault bots when economy allows or time forces it
        eco_ok = surplus_for_assault(ct)
        time_ok = rnd >= OFFENSE_MIN_ROUND
        if not eco_ok and not time_ok:
            return

        cap = min(GameConstants.MAX_TEAM_UNITS, 1 + OPENING_MINERS + TARGET_ASSAULT_BOTS)
        if (
            ct.get_unit_count() < cap
            and rnd - self.last_assault_round >= ASSAULT_COOLDOWN
        ):
            if self._spawn_assault(ct):
                self.last_assault_round = rnd

    def _spawn_at(self, ct: Controller, pos: Position, reserve: int) -> bool:
        cost = ct.get_builder_bot_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve):
            return False
        if ct.can_spawn(pos):
            ct.spawn_builder(pos)
            return True
        return False

    def _spawn_assault(self, ct: Controller) -> bool:
        assert self.assault_spawn_tiles
        n = len(self.assault_spawn_tiles)
        for off in range(n):
            idx = (self.assault_cursor + off) % n
            if self._spawn_at(ct, self.assault_spawn_tiles[idx], reserve=BASE_RESERVE):
                self.assault_cursor = (idx + 1) % n
                return True
        return False

    # ===================================================================
    # Builder dispatch and bootstrap
    # ===================================================================

    def _run_builder(self, ct: Controller) -> None:
        if not self.initialized:
            self._bootstrap_builder(ct)
        if self.role == "assault":
            self._run_assault(ct)
        else:
            self._run_miner(ct)

    def _bootstrap_builder(self, ct: Controller) -> None:
        if self.core_pos is None:
            self._bootstrap(ct)
        assert self.core_pos and self.lane_dirs and self.miner_spawn_tiles and self.enemy_guesses

        self.initial_pos = ct.get_position()

        if self.initial_pos in self.miner_spawn_tiles:
            self.lane_index = self.miner_spawn_tiles.index(self.initial_pos)
            self.role = f"miner_{self.lane_index}"
            self.sweep_sign = 1 if self.lane_index % 2 == 0 else -1
            self.home_dir = self.lane_dirs[self.lane_index % len(self.lane_dirs)]
            # Chain terminates at the tile directly adjacent to the core
            self.home_slot = step(self.core_pos, self.home_dir, 1)
            self.preferred_dir = self.home_dir
            self.miner_state = "find_ore"
        else:
            self.role = "assault"
            primary = self.core_pos.direction_to(self.enemy_guesses[0])
            exits = directional_preferences(primary)
            stage = ct.get_id() % 6
            self.assault_exit_dir = exits[stage % len(exits)]

        self.initialized = True

    # ===================================================================
    # Miner logic — explicit state machine
    # ===================================================================
    #
    # State transitions:
    #   find_ore
    #     → [found ore] → walk_to_stand
    #     → [no ore visible] drift in preferred_dir
    #   walk_to_stand
    #     → [arrived] → build_harvester
    #     → [ore gone] → find_ore
    #   build_harvester
    #     → [built] → build_chain
    #     → [ore gone] → find_ore
    #   build_chain
    #     → [chain complete] → find_ore  (look for next ore)
    #     → [chain failed] → find_ore

    def _run_miner(self, ct: Controller) -> None:
        s = self.miner_state
        if s == "find_ore":
            self._miner_find_ore(ct)
        elif s == "walk_to_stand":
            self._miner_walk_to_stand(ct)
        elif s == "build_harvester":
            self._miner_build_harvester(ct)
        elif s == "build_chain":
            self._miner_build_chain(ct)
        else:
            self.miner_state = "find_ore"

    # --- find_ore ---

    def _miner_find_ore(self, ct: Controller) -> None:
        """
        Scan visible tiles for unclaimed Ti ore.  Score by: how short the
        resulting conveyor chain would be (distance to home_slot) + how far we
        are from the stand.  Claim with a marker to prevent other miners
        targeting the same deposit.
        """
        assert self.core_pos and self.preferred_dir and self.home_slot
        current = ct.get_position()
        best_ore: Position | None = None
        best_stand: Position | None = None
        best_score = 10**9

        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
                continue
            bid = ct.get_tile_building_id(pos)
            if bid is not None:
                continue  # already has a building (harvester or otherwise)
            # Skip if a friendly marker signals another bot has claimed this ore
            # (We use can_place_marker to check — if it fails, something is there)
            stand = self._best_stand(ct, pos)
            if stand is None:
                continue
            # Prefer ore whose adjacent tile is close to home_slot (short chain)
            chain_est = pos.distance_squared(self.home_slot)
            stand_dist = current.distance_squared(stand)
            score = chain_est * 3 + stand_dist
            if score < best_score:
                best_ore = pos
                best_stand = stand
                best_score = score

        if best_ore is None:
            # No ore visible — sweep outward in preferred direction
            self._step_toward(ct, None, self._swept_dir())
            self.stall_turns += 1
            if self.stall_turns >= 8:
                # Rotate sweep direction to avoid looping
                self.preferred_dir = cardinal_right(self.preferred_dir)
                self.stall_turns = 0
            return

        self.target_ore = best_ore
        self.target_stand = best_stand
        self.stall_turns = 0
        # Claim with a marker on the ore tile
        if ct.can_place_marker(best_ore):
            ct.place_marker(best_ore)
        self.miner_state = "walk_to_stand"

    def _swept_dir(self) -> Direction:
        """Alternate between preferred_dir and its perpendicular to sweep the map."""
        assert self.preferred_dir
        if self.stall_turns % 12 < 6:
            return self.preferred_dir
        return cardinal_right(self.preferred_dir) if self.sweep_sign > 0 else cardinal_left(self.preferred_dir)

    # --- walk_to_stand ---

    def _miner_walk_to_stand(self, ct: Controller) -> None:
        assert self.target_ore and self.target_stand and self.preferred_dir
        current = ct.get_position()

        if not self._ore_valid(ct, self.target_ore):
            self.miner_state = "find_ore"
            return

        if current == self.target_stand:
            self.miner_state = "build_harvester"
            self.stall_turns = 0
            return

        moved = self._step_toward(ct, self.target_stand, self.preferred_dir)
        if moved:
            self.stall_turns = 0
        else:
            self.stall_turns += 1
            if self.stall_turns >= STALL_LIMIT:
                # Try a different stand position
                alt = self._best_stand(ct, self.target_ore, exclude=self.target_stand)
                if alt:
                    self.target_stand = alt
                self.stall_turns = 0

    # --- build_harvester ---

    def _miner_build_harvester(self, ct: Controller) -> None:
        assert self.target_ore and self.home_slot
        current = ct.get_position()

        if not self._ore_valid(ct, self.target_ore):
            self.miner_state = "find_ore"
            return

        # Must be cardinally adjacent to the ore
        if abs(current.x - self.target_ore.x) + abs(current.y - self.target_ore.y) != 1:
            self.miner_state = "walk_to_stand"
            return

        cost = ct.get_harvester_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_eco(ct)):
            return  # wait for funds

        if ct.can_build_harvester(self.target_ore):
            ct.build_harvester(self.target_ore)
            # Plan the conveyor chain from the tile we're on back to home_slot
            self.conv_chain = self._plan_miner_chain(ct, current, self.home_slot)
            self.conv_idx = 0
            self.miner_state = "build_chain"
            self.stall_turns = 0

    # --- build_chain ---

    def _miner_build_chain(self, ct: Controller) -> None:
        """
        Walk the planned chain and place conveyors at each tile pointing toward
        the next tile (toward the core).  The chain is:
          [stand (adjacent to harvester), c1, c2, ..., home_slot]
        Conveyor at index i points toward index i+1.
        The conveyor at home_slot points into the core.

        The harvester itself automatically outputs to an adjacent building,
        so the first conveyor (at the stand) receives from the harvester and
        passes it forward.
        """
        assert self.home_slot and self.preferred_dir

        if not self.conv_chain:
            self.miner_state = "find_ore"
            return

        # Chain fully placed?
        if self.conv_idx >= len(self.conv_chain) - 1:
            # Done — look for the next ore deposit
            self.target_ore = None
            self.target_stand = None
            self.conv_chain = None
            self.conv_idx = 0
            self.miner_state = "find_ore"
            return

        current = ct.get_position()
        pos = self.conv_chain[self.conv_idx]       # tile to place conveyor on
        dest = self.conv_chain[self.conv_idx + 1]  # tile it should point toward

        # Walk to within action radius of pos
        if current.distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
            self._step_toward(ct, pos, self.preferred_dir)
            return

        result = self._place_conveyor(ct, pos, dest)
        if result == "done":
            self.conv_idx += 1
            self.stall_turns = 0
        elif result == "wait":
            # Something needed to happen (e.g. destroyed wrong-direction conveyor)
            pass
        else:  # "blocked"
            self.stall_turns += 1
            if self.stall_turns >= STALL_LIMIT:
                # Replan from current position
                new_chain = self._plan_miner_chain(ct, pos, self.home_slot)
                if new_chain:
                    self.conv_chain = self.conv_chain[: self.conv_idx] + new_chain
                else:
                    self.miner_state = "find_ore"
                self.stall_turns = 0

    # --- miner helpers ---

    def _plan_miner_chain(
        self, ct: Controller, start: Position, goal: Position
    ) -> list[Position] | None:
        """
        BFS cardinal path from *start* to *goal*.  Tiles are passable if they
        are empty, have a walkable building, or are the goal itself.
        """
        assert self.width and self.height and self.core_pos
        w, h = self.width, self.height
        cp = self.core_pos

        def passable(p: Position) -> bool:
            if on_core_tile(p, cp):
                return True
            if not in_bounds(p, w, h):
                return False
            if not ct.is_in_vision(p):
                return True  # optimistically assume passable
            env = ct.get_tile_env(p)
            if env not in (Environment.EMPTY,) and not ct.is_tile_passable(p):
                return False
            bid = ct.get_tile_building_id(p)
            if bid is None:
                return True
            etype = ct.get_entity_type(bid)
            return etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER

        return plan_chain(start, goal, w, h, passable)

    def _place_conveyor(self, ct: Controller, pos: Position, dest: Position) -> str:
        """
        Ensure a conveyor at *pos* points toward *dest*.
        Returns: "done" | "wait" | "blocked"
        """
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.CONVEYOR:
                if ct.get_direction(bid) == pos.direction_to(dest):
                    return "done"
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                return "wait"
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                return "wait"
            return "blocked"

        cost = ct.get_conveyor_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
            return "wait"
        if ct.can_build_conveyor(pos, pos.direction_to(dest)):
            ct.build_conveyor(pos, pos.direction_to(dest))
            return "done"
        return "blocked"

    def _ore_valid(self, ct: Controller, pos: Position) -> bool:
        """An ore tile is valid if it's still ore and has no building on it."""
        if not ct.is_in_vision(pos):
            return True  # assume still valid
        if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
            return False
        return ct.get_tile_building_id(pos) is None

    def _best_stand(
        self,
        ct: Controller,
        ore_pos: Position,
        exclude: Position | None = None,
    ) -> Position | None:
        """Return the best cardinal neighbour of *ore_pos* to stand on when building."""
        assert self.core_pos and self.preferred_dir and self.width and self.height
        current = ct.get_position()
        candidates: list[Position] = []
        for d in CARDINAL_DIRECTIONS:
            p = ore_pos.add(d)
            if not in_bounds(p, self.width, self.height):
                continue
            if p == exclude:
                continue
            if on_core_tile(p, self.core_pos):
                continue
            if ct.is_in_vision(p):
                env = ct.get_tile_env(p)
                if env not in (Environment.EMPTY,) and not ct.is_tile_passable(p):
                    continue
                bid = ct.get_tile_building_id(p)
                if bid is not None and not ct.is_tile_passable(p):
                    continue
            candidates.append(p)
        if not candidates:
            return None
        candidates.sort(key=lambda p: (
            current.distance_squared(p),
            direction_rank(self.preferred_dir, current.direction_to(p)),
        ))
        return candidates[0]

    def _step_toward(
        self,
        ct: Controller,
        target: Position | None,
        fallback: Direction,
    ) -> bool:
        """
        Move one step toward *target* (or in *fallback* direction if None).
        Paves a road on empty tiles when needed.  Returns True if moved.
        """
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        if target is not None and current == target:
            return False

        prim = current.direction_to(target) if target is not None else fallback
        dirs = directional_preferences(prim, fallback if target is not None else None)

        for d in dirs:
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if ct.can_move(d):
                ct.move(d)
                return True
            # Try paving a road on an empty tile
            if ct.is_in_vision(dest):
                if ct.get_tile_env(dest) != Environment.EMPTY:
                    continue
                if ct.get_tile_building_id(dest) is not None:
                    continue
            cost = ct.get_road_cost()
            if can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
                if ct.can_build_road(dest):
                    ct.build_road(dest)
                    if ct.can_move(d):
                        ct.move(d)
                        return True
        return False

    # ===================================================================
    # Assault logic
    # ===================================================================

    def _run_assault(self, ct: Controller) -> None:
        assert self.enemy_guesses and self.core_pos
        current = ct.get_position()

        # Always fire on the building under us if it's enemy — cheap damage
        tile_bid = ct.get_tile_building_id(current)
        if tile_bid is not None and ct.get_team(tile_bid) != ct.get_team():
            if ct.can_fire(current):
                ct.fire(current)
                return

        # Destroy adjacent enemy conveyors/harvesters to cut their income
        if self._destroy_enemy_economy(ct):
            return

        self._scan_enemy_core(ct)

        target = self.enemy_core_pos or self.enemy_guesses[
            self.assault_guess_index % len(self.enemy_guesses)
        ]

        if self.enemy_core_pos is not None:
            # Try to build a siege gunner
            if self._try_siege_gunner(ct):
                return
            # Try to feed an existing gunner with Ti
            if self._try_ammo_chain(ct):
                return

        # Advance guess if we've reached it without finding the core
        if self.enemy_core_pos is None and current.distance_squared(target) <= 16:
            self._advance_guess()
            target = self.enemy_guesses[self.assault_guess_index % len(self.enemy_guesses)]

        # Break out of home base area first
        if self._breakout(ct):
            self.stall_turns = 0
            return

        moved = self._flow_move(ct, target) or self._greedy_move(ct, target)
        if moved:
            self.stall_turns = 0
            return

        self.stall_turns += 1
        if self.stall_turns >= STALL_LIMIT:
            if self._try_build_launcher(ct):
                self.stall_turns = 0

    # --- destroy enemy economy ---

    def _destroy_enemy_economy(self, ct: Controller) -> bool:
        """
        Destroy the highest-value enemy building within action radius.
        Priority: harvesters > conveyors > roads (harvesters are most expensive).
        """
        current = ct.get_position()
        priority = {
            EntityType.HARVESTER: 0,
            EntityType.CONVEYOR: 1,
            EntityType.ARMOURED_CONVEYOR: 1,
            EntityType.BRIDGE: 1,
            EntityType.ROAD: 2,
        }
        best: tuple[int, Position] | None = None
        for pos in ct.get_nearby_tiles():
            if current.distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
                continue
            bid = ct.get_tile_building_id(pos)
            if bid is None or ct.get_team(bid) == ct.get_team():
                continue
            etype = ct.get_entity_type(bid)
            pri = priority.get(etype)
            if pri is None:
                continue
            if best is None or pri < best[0]:
                best = (pri, pos)
        if best is not None:
            if ct.can_destroy(best[1]):
                ct.destroy(best[1])
                return True
        return False

    # --- breakout ---

    def _breakout(self, ct: Controller) -> bool:
        assert self.core_pos
        current = ct.get_position()
        if current.distance_squared(self.core_pos) > 25:
            return False
        if self.assault_exit_dir is None:
            return False
        target = self.core_pos
        for _ in range(6):
            nxt = target.add(self.assault_exit_dir)
            if not in_bounds(nxt, self.width or 0, self.height or 0):
                break
            target = nxt
        return self._greedy_move(ct, target)

    # --- flow-field navigation ---

    def _nav_ok(self, ct: Controller, pos: Position) -> bool:
        assert self.core_pos and self.width and self.height
        if not in_bounds(pos, self.width, self.height):
            return False
        if on_core_tile(pos, self.core_pos):
            return True
        if not ct.is_in_vision(pos):
            return True
        if ct.is_tile_passable(pos):
            return True
        if ct.get_tile_env(pos) != Environment.EMPTY:
            return False
        if ct.get_tile_builder_bot_id(pos) is not None:
            return False
        bid = ct.get_tile_building_id(pos)
        if bid is None:
            return True
        etype = ct.get_entity_type(bid)
        return etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER

    def _get_flow(self, ct: Controller, target: Position) -> dict[tuple[int, int], int] | None:
        assert self.enemy_guesses and self.width and self.height
        goals: list[Position] = []

        if self.enemy_core_pos is not None:
            _, gtiles = self._siege_positions()
            for p in gtiles:
                if self._nav_ok(ct, p) and p not in goals:
                    goals.append(p)

        for r in (4, 3, 5, 2, 6):
            for p in ring_positions(target, r, self.width, self.height):
                if not on_core_tile(p, target) and self._nav_ok(ct, p) and p not in goals:
                    goals.append(p)

        if not goals:
            return None

        sig = (target.x, target.y, len(goals), self.enemy_core_pos is not None)
        if self.flow_sig == sig and self.flow is not None:
            return self.flow

        flow: dict[tuple[int, int], int] = {}
        q: deque[Position] = deque()
        for p in goals:
            k = (p.x, p.y)
            if k not in flow:
                flow[k] = 0
                q.append(p)
        while q:
            p = q.popleft()
            base = flow[(p.x, p.y)]
            for d in DIRECTIONS:
                nxt = p.add(d)
                k = (nxt.x, nxt.y)
                if k in flow or not self._nav_ok(ct, nxt):
                    continue
                flow[k] = base + 1
                q.append(nxt)

        self.flow_sig = sig
        self.flow = flow
        return flow

    def _flow_move(self, ct: Controller, target: Position) -> bool:
        flow = self._get_flow(ct, target)
        if not flow:
            return False
        current = ct.get_position()
        ck = (current.x, current.y)
        cs = flow.get(ck)
        if cs is None:
            self.flow_sig = None
            self.flow = None
            flow = self._get_flow(ct, target)
            if not flow:
                return False
            cs = flow.get(ck)
            if cs is None:
                return False

        primary = current.direction_to(target)
        options: list[tuple[tuple, Direction]] = []
        for d in directional_preferences(primary):
            dest = current.add(d)
            k = (dest.x, dest.y)
            s = flow.get(k)
            if s is None:
                continue
            occ = ct.get_tile_builder_bot_id(dest) is not None if ct.is_in_vision(dest) else False
            pas = ct.is_tile_passable(dest) if ct.is_in_vision(dest) else False
            rank = (s, 1 if occ else 0, 0 if pas else 1, direction_rank(primary, d))
            options.append((rank, d))

        for _, d in sorted(options):
            if self._try_step(ct, d):
                return True

        self.flow_sig = None
        self.flow = None
        return False

    def _greedy_move(self, ct: Controller, target: Position) -> bool:
        assert self.width and self.height
        current = ct.get_position()
        if current == target:
            return False
        for d in directional_preferences(current.direction_to(target)):
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if self._try_step(ct, d):
                return True
        return False

    def _try_step(self, ct: Controller, d: Direction) -> bool:
        dest = ct.get_position().add(d)
        if ct.can_move(d):
            ct.move(d)
            return True
        if ct.is_in_vision(dest):
            if ct.get_tile_env(dest) != Environment.EMPTY:
                return False
            if ct.get_tile_building_id(dest) is not None:
                return False
        cost = ct.get_road_cost()
        reserve = BASE_RESERVE // 2 if self.role == "assault" else BASE_RESERVE
        if can_afford(ct, cost[0], cost[1], reserve=reserve):
            if ct.can_build_road(dest):
                ct.build_road(dest)
                if ct.can_move(d):
                    ct.move(d)
                    return True
        return False

    def _advance_guess(self) -> None:
        assert self.enemy_guesses
        if len(self.enemy_guesses) > 1:
            self.assault_guess_index = (self.assault_guess_index + 1) % len(self.enemy_guesses)
        self.flow_sig = None
        self.flow = None

    def _scan_enemy_core(self, ct: Controller) -> None:
        for eid in ct.get_nearby_buildings():
            if ct.get_entity_type(eid) == EntityType.CORE and ct.get_team(eid) != ct.get_team():
                self.enemy_core_pos = ct.get_position(eid)
                return

    # --- siege gunner placement ---

    def _try_siege_gunner(self, ct: Controller) -> bool:
        """
        Place gunners on cardinal positions at distance 2 from the enemy core.
        These have a direct line to the core tile in their facing direction.
        Also cover distance-2 corners and distance-3 ring for extra bots.
        """
        assert self.enemy_core_pos and self.width and self.height
        current = ct.get_position()
        _, gtiles = self._siege_positions()

        for pos in gtiles:
            if current.distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
                continue
            etype = building_type_at(ct, pos)
            if etype == EntityType.GUNNER:
                continue
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return True
                continue
            if etype is not None:
                continue  # some other building
            face = pos.direction_to(self.enemy_core_pos)
            if face == Direction.CENTRE:
                continue
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                if ct.can_build_gunner(pos, face):
                    ct.build_gunner(pos, face)
                    return True

        # Move toward nearest unfilled slot
        open_slots = [
            p for p in gtiles
            if not ct.is_in_vision(p) or ct.get_tile_building_id(p) is None
        ]
        if open_slots:
            t = min(open_slots, key=lambda p: current.distance_squared(p))
            return self._greedy_move(ct, t)
        return False

    def _siege_positions(self) -> tuple[list[Position], list[Position]]:
        """
        Cardinal-2 positions first (best direct fire), then full ring-2 and ring-3.
        """
        assert self.enemy_core_pos and self.width and self.height
        ec = self.enemy_core_pos
        w, h = self.width, self.height

        cardinal_2 = [
            Position(ec.x, ec.y - 2),
            Position(ec.x, ec.y + 2),
            Position(ec.x - 2, ec.y),
            Position(ec.x + 2, ec.y),
        ]
        ring2 = ring_positions(ec, 2, w, h)
        ring3 = ring_positions(ec, 3, w, h)
        all_g = cardinal_2 + [p for p in ring2 if p not in cardinal_2] + ring3

        seen: list[Position] = []
        for p in all_g:
            if in_bounds(p, w, h) and not on_core_tile(p, ec) and p not in seen:
                seen.append(p)
        return [], seen

    # --- ammo chain: Ti harvester → conveyor → gunner ---

    def _try_ammo_chain(self, ct: Controller) -> bool:
        """
        Find an unfed siege gunner, build a Ti harvester on the nearest ore,
        and run a conveyor chain from that harvester into a valid (non-facing)
        side of the gunner so it fires every round.

        A gunner accepts ammo from all sides except the side it faces.
        """
        assert self.enemy_core_pos and self.width and self.height
        current = ct.get_position()
        w, h = self.width, self.height

        # 1. Pick an unfed friendly gunner near the enemy core
        if self.ammo_gunner_pos is None:
            best_g: Position | None = None
            best_score = 10**9
            for pos in ct.get_nearby_tiles():
                if not ct.is_in_vision(pos):
                    continue
                bid = ct.get_tile_building_id(pos)
                if bid is None or ct.get_entity_type(bid) != EntityType.GUNNER:
                    continue
                if ct.get_team(bid) != ct.get_team():
                    continue
                # Check if already fed by a conveyor
                fed = False
                for adj in adjacent_cardinals(pos):
                    abid = ct.get_tile_building_id(adj)
                    if abid and ct.get_entity_type(abid) == EntityType.CONVEYOR:
                        if adj.add(ct.get_direction(abid)) == pos:
                            fed = True
                            break
                if fed:
                    continue
                score = (
                    pos.distance_squared(self.enemy_core_pos)
                    + current.distance_squared(pos)
                )
                if score < best_score:
                    best_score = score
                    best_g = pos
            if best_g is None:
                return False
            self.ammo_gunner_pos = best_g
            self.ammo_chain = None
            self.ammo_chain_idx = 0

        gpos = self.ammo_gunner_pos

        # Verify gunner still exists
        if ct.is_in_vision(gpos):
            gbid = ct.get_tile_building_id(gpos)
            if (gbid is None or ct.get_entity_type(gbid) != EntityType.GUNNER
                    or ct.get_team(gbid) != ct.get_team()):
                self.ammo_gunner_pos = None
                return False
            gunner_face = ct.get_direction(gbid)
        else:
            gunner_face = gpos.direction_to(self.enemy_core_pos)

        # 2. Find the nearest visible Ti ore
        best_ore: Position | None = None
        best_ore_score = 10**9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
                continue
            ebid = ct.get_tile_building_id(pos)
            if ebid is not None and ct.get_entity_type(ebid) != EntityType.HARVESTER:
                continue
            score = pos.distance_squared(gpos) * 3 + current.distance_squared(pos)
            if score < best_ore_score:
                best_ore = pos
                best_ore_score = score

        if best_ore is None:
            return False

        # 3. Build harvester if not yet built
        ore_bid = ct.get_tile_building_id(best_ore)
        if ore_bid is None:
            cost = ct.get_harvester_cost()
            if not can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                return False
            dist_man = abs(current.x - best_ore.x) + abs(current.y - best_ore.y)
            if current.distance_squared(best_ore) <= GameConstants.ACTION_RADIUS_SQ and dist_man == 1:
                if ct.can_build_harvester(best_ore):
                    ct.build_harvester(best_ore)
                    self.ammo_chain = None
                    return True
            stand = self._assault_stand(ct, best_ore)
            if stand:
                return self._greedy_move(ct, stand)
            return False

        # 4. Determine a valid feed side on the gunner (not the facing side)
        feed_side: Position | None = None
        for adj in adjacent_cardinals(gpos):
            if not in_bounds(adj, w, h):
                continue
            dir_to_gunner = adj.direction_to(gpos)
            if dir_to_gunner == gunner_face:
                continue  # gunner faces this way — can't receive from here
            abid = ct.get_tile_building_id(adj)
            if abid:
                etype = ct.get_entity_type(abid)
                if etype not in (EntityType.CONVEYOR, *WALKABLE_BUILDINGS, EntityType.MARKER):
                    continue
            feed_side = adj
            break

        if feed_side is None:
            return False

        # 5. Plan chain from ore to feed_side
        if self.ammo_chain is None:
            cp = self.core_pos

            def passable(p: Position) -> bool:
                if cp and on_core_tile(p, cp):
                    return True
                if not in_bounds(p, w, h):
                    return False
                if not ct.is_in_vision(p):
                    return True
                env = ct.get_tile_env(p)
                if env not in (Environment.EMPTY,) and not ct.is_tile_passable(p):
                    return False
                bid2 = ct.get_tile_building_id(p)
                if bid2 is None:
                    return True
                etype = ct.get_entity_type(bid2)
                return etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER

            full_chain = plan_chain(best_ore, feed_side, w, h, passable)
            if full_chain and len(full_chain) >= 2:
                # Skip index 0 (that's the ore tile itself — the harvester is there)
                self.ammo_chain = full_chain[1:]
                self.ammo_chain_idx = 0
            else:
                return self._greedy_move(ct, feed_side)

        chain = self.ammo_chain
        if not chain or self.ammo_chain_idx >= len(chain) - 1:
            # Chain complete — move on to feed the next gunner
            self.ammo_gunner_pos = None
            self.ammo_chain = None
            return False

        pos = chain[self.ammo_chain_idx]
        dest = chain[self.ammo_chain_idx + 1]

        if current.distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
            return self._greedy_move(ct, pos)

        result = self._place_conveyor(ct, pos, dest)
        if result == "done":
            self.ammo_chain_idx += 1
            return True
        if result == "wait":
            return True
        return False

    def _assault_stand(self, ct: Controller, ore_pos: Position) -> Position | None:
        assert self.width and self.height
        current = ct.get_position()
        best: Position | None = None
        best_d = 10**9
        for d in CARDINAL_DIRECTIONS:
            p = ore_pos.add(d)
            if not in_bounds(p, self.width, self.height):
                continue
            if ct.is_in_vision(p):
                if ct.get_tile_env(p) not in (Environment.EMPTY,) and not ct.is_tile_passable(p):
                    continue
                bid = ct.get_tile_building_id(p)
                if bid and not ct.is_tile_passable(p):
                    continue
            dist = current.distance_squared(p)
            if dist < best_d:
                best_d = dist
                best = p
        return best

    def _try_build_launcher(self, ct: Controller) -> bool:
        assert self.enemy_guesses and self.width and self.height
        target = self.enemy_core_pos or self.enemy_guesses[
            self.assault_guess_index % len(self.enemy_guesses)
        ]
        current = ct.get_position()
        for d in directional_preferences(current.direction_to(target)):
            bp = current.add(d)
            if not in_bounds(bp, self.width, self.height):
                continue
            if ct.get_tile_building_id(bp) is not None:
                continue
            if ct.is_in_vision(bp) and ct.get_tile_env(bp) != Environment.EMPTY:
                continue
            cost = ct.get_launcher_cost()
            if not can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                return False
            if ct.can_build_launcher(bp):
                ct.build_launcher(bp)
                return True
        return False

    # ===================================================================
    # Gunner / Launcher logic
    # ===================================================================

    def _run_gunner(self, ct: Controller) -> None:
        target = ct.get_gunner_target()
        if target is None:
            return
        # Attack builder bots first (mobile targets), then buildings
        bot_id = ct.get_tile_builder_bot_id(target)
        if bot_id is not None and ct.get_team(bot_id) != ct.get_team():
            if ct.can_fire(target):
                ct.fire(target)
            return
        bid = ct.get_tile_building_id(target)
        if bid is not None and ct.get_team(bid) != ct.get_team():
            if ct.can_fire(target):
                ct.fire(target)

    def _run_launcher(self, ct: Controller) -> None:
        assert self.enemy_guesses
        target = self.enemy_core_pos or self.enemy_guesses[0]
        for eid in ct.get_nearby_buildings():
            if ct.get_entity_type(eid) == EntityType.CORE and ct.get_team(eid) != ct.get_team():
                target = ct.get_position(eid)
                break

        allies = [
            bid for bid in ct.get_nearby_builder_bots()
            if ct.get_team(bid) == ct.get_team()
            and ct.get_position(bid).distance_squared(ct.get_position()) <= 2
        ]
        if not allies:
            return

        # Prioritise launching directly to enemy core tiles
        if self.enemy_core_pos is not None and self.width and self.height:
            for bot_id in allies:
                bpos = ct.get_position(bot_id)
                for ct_tile in core_tiles(self.enemy_core_pos, self.width, self.height):
                    if ct.can_launch(bpos, ct_tile):
                        ct.launch(bpos, ct_tile)
                        return

        candidates = sorted(
            [
                p for p in ct.get_nearby_tiles(26)
                if ct.is_tile_passable(p)
                and ct.get_tile_builder_bot_id(p) is None
                and p.distance_squared(target) < ct.get_position().distance_squared(target)
            ],
            key=lambda p: (
                p.distance_squared(target),
                -ct.get_position().distance_squared(p),
            ),
        )
        for bot_id in allies:
            bpos = ct.get_position(bot_id)
            for p in candidates:
                if ct.can_launch(bpos, p):
                    ct.launch(bpos, p)
                    return
