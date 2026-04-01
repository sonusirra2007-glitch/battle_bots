from __future__ import annotations

from collections import deque

import a as base
from cambc import Controller, Direction, EntityType, Environment, GameConstants, Position

# Keep the original titanium-only rush identity, but bias the inherited bot
# toward a fixed 12-bot symmetry split and a slightly faster midgame flood.
base.TARGET_ASSAULT_BOTS = 12
base.OFFENSE_MIN_ROUND = 72
base.ASSAULT_SPAWN_COOLDOWN = 4
base.ASSAULT_SURPLUS = 90

PRIMARY_ASSAULT_FORCE = 12
EXPECTED_ASSAULT_TURRETS = 6
ASSAULT_ASSIGN_MARKER = 0xA5500000
ASSAULT_ASSIGN_MASK = 0xFFFF0000
ORE_CLAIM_MARKER = 0xA5600000
ORE_CLAIM_MASK = 0xFFF00000
ORE_CLAIM_ROUND_MASK = 0x7FF
ORE_CLAIM_OWNER_MASK = 0x7FF
ORE_CLAIM_TTL = 18
GUESS_STATUS_MARKER = 0xA5700000
GUESS_STATUS_MASK = 0xFFFF0000
GUESS_STATUS_CONFIRMED = 1 << 8


def encode_assault_assignment(guess_index: int) -> int:
    return ASSAULT_ASSIGN_MARKER | (guess_index & 0xFF)


def decode_assault_assignment(value: int) -> int | None:
    if (value & ASSAULT_ASSIGN_MASK) != ASSAULT_ASSIGN_MARKER:
        return None
    return value & 0xFF


def encode_ore_claim(round_num: int, owner: int) -> int:
    return (
        ORE_CLAIM_MARKER
        | ((round_num & ORE_CLAIM_ROUND_MASK) << 11)
        | (owner & ORE_CLAIM_OWNER_MASK)
    )


def decode_ore_claim(value: int) -> tuple[int, int] | None:
    if (value & ORE_CLAIM_MASK) != ORE_CLAIM_MARKER:
        return None
    return ((value >> 11) & ORE_CLAIM_ROUND_MASK, value & ORE_CLAIM_OWNER_MASK)


def encode_guess_status(guess_index: int, confirmed: bool) -> int:
    return GUESS_STATUS_MARKER | (guess_index & 0xFF) | (GUESS_STATUS_CONFIRMED if confirmed else 0)


def decode_guess_status(value: int) -> tuple[int, bool] | None:
    if (value & GUESS_STATUS_MASK) != GUESS_STATUS_MARKER:
        return None
    return (value & 0xFF, bool(value & GUESS_STATUS_CONFIRMED))


class Player(base.Player):
    def __init__(self):
        super().__init__()
        self.assault_spawned_total = 0
        self.assigned_guess_index: int | None = None
        self.secondary_routes_built = 0
        self.home_splitter_pos: Position | None = None
        self.home_splitter_dir: Direction | None = None
        self.dead_guess_indices: set[int] = set()
        self.confirmed_guess_index: int | None = None

        self.ammo_ore_pos: Position | None = None
        self.ammo_stand_pos: Position | None = None
        self.ammo_feed_pos: Position | None = None
        self.ammo_path: list[Position] = []
        self.last_ammo_replan_round = -999

    # -------------------------------------------------------------------
    # Shared bootstrap / symmetry pruning
    # -------------------------------------------------------------------

    def bootstrap_common(self, ct: Controller) -> None:
        super().bootstrap_common(ct)
        if self.core_pos is None or self.width is None or self.height is None:
            return

        self.enemy_guesses = self.refined_enemy_guesses(ct)

        miner_tiles = list(self.miner_spawn_tiles or [])
        outer_core_tiles = [
            Position(self.core_pos.x + dx, self.core_pos.y + dy)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if (dx != 0 or dy != 0)
            and base.in_bounds(
                Position(self.core_pos.x + dx, self.core_pos.y + dy),
                self.width,
                self.height,
            )
        ]
        used = set(miner_tiles)
        assault_tiles = [pos for pos in outer_core_tiles if pos not in used]
        self.assault_spawn_tiles = assault_tiles or outer_core_tiles or miner_tiles

    def refined_enemy_guesses(self, ct: Controller) -> list[Position]:
        assert self.core_pos is not None and self.width is not None and self.height is not None
        width = self.width
        height = self.height
        core_pos = self.core_pos

        candidates = [
            (
                Position(width - 1 - core_pos.x, height - 1 - core_pos.y),
                lambda pos: Position(width - 1 - pos.x, height - 1 - pos.y),
            ),
            (
                Position(width - 1 - core_pos.x, core_pos.y),
                lambda pos: Position(width - 1 - pos.x, pos.y),
            ),
            (
                Position(core_pos.x, height - 1 - core_pos.y),
                lambda pos: Position(pos.x, height - 1 - pos.y),
            ),
        ]

        visible_tiles = ct.get_nearby_tiles()
        guesses: list[Position] = []
        seen: set[tuple[int, int]] = set()
        for guess, transform in candidates:
            key = (guess.x, guess.y)
            if guess == core_pos or key in seen:
                continue
            seen.add(key)
            consistent = True
            for pos in visible_tiles:
                mirror = transform(pos)
                if not base.in_bounds(mirror, width, height):
                    consistent = False
                    break
                if not ct.is_in_vision(mirror):
                    continue
                if ct.get_tile_env(pos) != ct.get_tile_env(mirror):
                    consistent = False
                    break
            if consistent:
                guesses.append(guess)

        return guesses or base.symmetry_guesses(core_pos, width, height)

    # -------------------------------------------------------------------
    # Core logic
    # -------------------------------------------------------------------

    def run_core(self, ct: Controller) -> None:
        assert self.miner_spawn_tiles is not None and self.assault_spawn_tiles is not None
        current_round = ct.get_current_round()

        if self.opening_spawned < base.OPENING_MINERS:
            tile = self.miner_spawn_tiles[self.opening_spawned % len(self.miner_spawn_tiles)]
            if self.try_spawn_specific(ct, tile, reserve=base.BASE_RESERVE):
                self.opening_spawned += 1
            return

        missing_miner = self.first_missing_lane(ct)
        if missing_miner is not None and self.opening_spawned < len(self.lane_dirs or []):
            tile = self.miner_spawn_tiles[missing_miner % len(self.miner_spawn_tiles)]
            if self.try_spawn_specific(ct, tile, reserve=base.reserve_economy(ct)):
                self.opening_spawned += 1
            return

        desired_total = min(
            GameConstants.MAX_TEAM_UNITS,
            1 + base.OPENING_MINERS + PRIMARY_ASSAULT_FORCE + EXPECTED_ASSAULT_TURRETS,
        )
        can_spawn_assault = (
            ct.get_unit_count() < desired_total
            and (current_round >= base.OFFENSE_MIN_ROUND or base.titanium_surplus_for_assault(ct))
            and base.titanium_surplus_for_assault(ct)
            and current_round - self.last_assault_spawn_round >= base.ASSAULT_SPAWN_COOLDOWN
        )
        if can_spawn_assault and self.try_spawn_assault(ct):
            self.last_assault_spawn_round = current_round

    def first_missing_lane(self, ct: Controller) -> int | None:
        assert self.core_pos is not None and self.lane_dirs is not None
        for i, direction in enumerate(self.lane_dirs):
            pos = base.slot_for(self.core_pos, direction)
            entity_type = base.building_type_at(ct, pos)
            if entity_type not in (*base.ROUTABLE_ENDPOINTS, EntityType.SPLITTER):
                return i
        return None

    def try_spawn_assault(self, ct: Controller) -> bool:
        assert (
            self.assault_spawn_tiles is not None
            and self.enemy_guesses is not None
            and self.core_pos is not None
        )
        if not self.assault_spawn_tiles:
            return False

        guess_count = max(1, len(self.enemy_guesses))
        assigned_guess = self.assault_spawned_total % guess_count
        target = self.enemy_guesses[assigned_guess]
        ordered_tiles = sorted(
            self.assault_spawn_tiles,
            key=lambda pos: (pos.distance_squared(target), pos.x, pos.y),
        )

        for pos in ordered_tiles:
            self.write_assault_assignment(ct, pos, assigned_guess)
            if self.try_spawn_specific(ct, pos, reserve=base.reserve_economy(ct)):
                self.assault_spawned_total += 1
                return True
        return False

    def spawn_witness_tile(self, spawn_pos: Position) -> Position | None:
        if self.core_pos is None or self.width is None or self.height is None:
            return None
        outward = self.core_pos.direction_to(spawn_pos)
        if outward == Direction.CENTRE:
            return None
        witness = spawn_pos.add(outward)
        if not base.in_bounds(witness, self.width, self.height):
            return None
        return witness

    def write_assault_assignment(self, ct: Controller, spawn_pos: Position, guess_index: int) -> None:
        witness = self.spawn_witness_tile(spawn_pos)
        if witness is None:
            return
        if ct.can_place_marker(witness):
            ct.place_marker(witness, encode_assault_assignment(guess_index))

    def read_assault_assignment(self, ct: Controller, spawn_pos: Position) -> int | None:
        witness = self.spawn_witness_tile(spawn_pos)
        if witness is None or not ct.is_in_vision(witness):
            return None
        building_id = ct.get_tile_building_id(witness)
        if building_id is None or ct.get_entity_type(building_id) != EntityType.MARKER:
            return None
        return decode_assault_assignment(ct.get_marker_value(building_id))

    # -------------------------------------------------------------------
    # Builder bootstrap / miner reassignment
    # -------------------------------------------------------------------

    def bootstrap_builder(self, ct: Controller) -> None:
        if self.initialized:
            return
        if self.core_pos is None:
            self.bootstrap_common(ct)

        assert (
            self.core_pos is not None
            and self.lane_dirs is not None
            and self.enemy_guesses is not None
            and self.miner_spawn_tiles is not None
        )

        self.initial_pos = ct.get_position()

        if self.initial_pos in self.miner_spawn_tiles:
            self.lane_index = self.miner_spawn_tiles.index(self.initial_pos)
            self.role = f"miner_{self.lane_index}"
            self.sweep_sign = -1 if self.lane_index % 2 == 0 else 1
            self.home_dir = self.lane_dirs[self.lane_index % len(self.lane_dirs)]
            self.home_splitter_pos = base.slot_for(self.core_pos, self.home_dir)
            self.home_splitter_dir = base.cardinal_opposite(self.home_dir)
            self.home_slot = self.home_splitter_pos
            self.home_target = base.slot_target_for(self.core_pos, self.home_dir)
            self.preferred_dir = self.home_dir
        else:
            self.role = "assault"
            assigned = self.read_assault_assignment(ct, self.initial_pos)
            if assigned is None or assigned >= len(self.enemy_guesses):
                assigned = ct.get_id() % max(1, len(self.enemy_guesses))
            self.assigned_guess_index = assigned
            self.assault_guess_index = assigned
            primary = self.core_pos.direction_to(self.enemy_guesses[assigned])
            exit_dirs = base.directional_preferences(primary)
            stage = ct.get_id() % max(1, min(5, len(exit_dirs)))
            self.assault_exit_dir = exit_dirs[stage]

        self.initialized = True

    def assign_miner_mission(self, ct: Controller) -> None:
        if not self.primary_titanium_done:
            self.start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="primary_titanium",
            )
            return

        if self.home_dir is None:
            return

        self.start_mission(
            target_env=Environment.ORE_TITANIUM,
            sink_pos=self.home_slot,
            sink_target=self.home_target,
            preferred_dir=self.home_dir,
            mission="secondary_titanium",
        )

    def execute_searching(self, ct: Controller) -> None:
        assert self.sink_pos is not None and self.sink_target is not None and self.preferred_dir is not None
        current = ct.get_position()

        if not self.trail:
            if current == self.sink_pos:
                self.trail = [current]
            else:
                self.move_to_sink(ct)
                return

        if self.target_ore is None or not self.ore_target_still_valid(ct, self.target_ore):
            self.target_ore, self.target_stand = self.plan_visible_ore(ct)
        elif self.target_ore is not None:
            self.target_stand = self.find_build_stand(ct, self.target_ore)

        if self.target_ore is not None:
            self.refresh_ore_claim(ct, self.target_ore)
            if current.distance_squared(self.target_ore) <= GameConstants.ACTION_RADIUS_SQ:
                if abs(current.x - self.target_ore.x) + abs(current.y - self.target_ore.y) != 1:
                    if not self.move_towards(ct, self.target_stand, self.preferred_dir):
                        self.stall_turns += 1
                        if self.stall_turns >= 2:
                            self.target_ore = None
                            self.target_stand = None
                            self.preferred_dir = base.cardinal_right(self.preferred_dir)
                            self.stall_turns = 0
                    else:
                        self.stall_turns = 0
                    return

                cost = ct.get_harvester_cost()
                if (
                    base.can_afford(ct, cost[0], cost[1], reserve=base.reserve_economy(ct))
                    and ct.can_build_harvester(self.target_ore)
                ):
                    ct.build_harvester(self.target_ore)
                    self.mission = f"{self.mission}_connecting"
                return

            move_target = self.target_stand if self.target_stand is not None else self.target_ore
            if not self.move_towards(ct, move_target, self.preferred_dir):
                self.stall_turns += 1
                if self.stall_turns >= 2:
                    self.target_ore = None
                    self.target_stand = None
                    self.preferred_dir = base.cardinal_right(self.preferred_dir)
                    self.stall_turns = 0
            else:
                self.stall_turns = 0
            return

        if not self.move_towards(ct, None, self.swept_direction()):
            self.stall_turns += 1
            if self.stall_turns >= 2:
                self.preferred_dir = base.cardinal_right(self.preferred_dir)
                self.stall_turns = 0
        else:
            self.stall_turns = 0

    def execute_connecting(self, ct: Controller) -> None:
        assert self.sink_target is not None
        if not self.trail:
            self.mission = "idle"
            return

        current = ct.get_position()
        if current == self.sink_target:
            if not self.ensure_lane_hub(ct):
                return
            if self.mission.startswith("primary_titanium"):
                self.primary_titanium_done = True
            elif self.mission.startswith("secondary_titanium"):
                self.secondary_routes_built += 1
                self.sweep_sign *= -1
            self.mission = "idle"
            return

        if current not in self.trail:
            return

        idx = self.trail.index(current)
        if idx < len(self.trail) - 1:
            if not self.ensure_transport_link(ct, self.trail[idx + 1], current):
                return

        if idx > 0:
            move_dir = current.direction_to(self.trail[idx - 1])
            if ct.can_move(move_dir):
                ct.move(move_dir)
            return

        move_dir = current.direction_to(self.sink_target)
        if ct.can_move(move_dir):
            ct.move(move_dir)

    def lane_side_positions(self) -> list[Position]:
        if self.home_splitter_pos is None or self.home_splitter_dir is None:
            return []
        return [
            self.home_splitter_pos.add(base.cardinal_left(self.home_splitter_dir)),
            self.home_splitter_pos.add(base.cardinal_right(self.home_splitter_dir)),
        ]

    def ensure_lane_hub(self, ct: Controller) -> bool:
        if self.home_splitter_pos is None or self.home_splitter_dir is None:
            return self.ensure_transport_link(ct, self.trail[0], self.sink_target)

        if not self.ensure_splitter_at(ct, self.home_splitter_pos, self.home_splitter_dir):
            return False

        for pos in self.lane_side_positions():
            if not self.ensure_side_core_link(ct, pos):
                return False

        return True

    def ensure_splitter_at(self, ct: Controller, pos: Position, direction: Direction) -> bool:
        building_id = ct.get_tile_building_id(pos) if ct.is_in_vision(pos) else None
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.SPLITTER and ct.get_direction(building_id) == direction:
                return True
            if entity_type == EntityType.MARKER:
                pass
            elif entity_type in base.WALKABLE_BUILDINGS and ct.get_team(building_id) == ct.get_team():
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return False
                return False
            else:
                return False

        cost = ct.get_splitter_cost()
        if not base.can_afford(ct, cost[0], cost[1], reserve=base.BASE_RESERVE):
            return False
        if ct.can_build_splitter(pos, direction):
            ct.build_splitter(pos, direction)
            return True
        return False

    def ensure_side_core_link(self, ct: Controller, pos: Position) -> bool:
        if (
            self.home_splitter_dir is None
            or self.width is None
            or self.height is None
            or not base.in_bounds(pos, self.width, self.height)
            or base.on_core_tile(pos, self.core_pos)
        ):
            return True
        return self.ensure_transport_link(ct, pos, pos.add(self.home_splitter_dir))

    def claim_owner_token(self, ct: Controller) -> int:
        return ct.get_id() & ORE_CLAIM_OWNER_MASK

    def marker_value_at(self, ct: Controller, pos: Position) -> int | None:
        if not ct.is_in_vision(pos):
            return None
        building_id = ct.get_tile_building_id(pos)
        if building_id is None or ct.get_entity_type(building_id) != EntityType.MARKER:
            return None
        if ct.get_team(building_id) != ct.get_team():
            return None
        return ct.get_marker_value(building_id)

    def recent_ore_claim_owner(self, ct: Controller, pos: Position) -> int | None:
        claim_tile = self.ore_claim_tile(ct, pos)
        if claim_tile is None:
            return None
        marker_value = self.marker_value_at(ct, claim_tile)
        if marker_value is None:
            return None
        decoded = decode_ore_claim(marker_value)
        if decoded is None:
            return None
        marked_round, owner = decoded
        current_round = ct.get_current_round() & ORE_CLAIM_ROUND_MASK
        age = (current_round - marked_round) & ORE_CLAIM_ROUND_MASK
        if age > ORE_CLAIM_TTL:
            return None
        return owner

    def refresh_ore_claim(self, ct: Controller, pos: Position) -> None:
        claim_tile = self.ore_claim_tile(ct, pos)
        if claim_tile is None:
            return
        building_id = ct.get_tile_building_id(claim_tile)
        if building_id is not None and ct.get_entity_type(building_id) not in (EntityType.MARKER,):
            return
        if ct.can_place_marker(claim_tile):
            ct.place_marker(
                claim_tile,
                encode_ore_claim(ct.get_current_round(), self.claim_owner_token(ct)),
            )

    def ore_claim_tile(self, ct: Controller, ore_pos: Position) -> Position | None:
        if self.width is None or self.height is None:
            return None
        for direction in (
            Direction.NORTHEAST,
            Direction.NORTHWEST,
            Direction.SOUTHEAST,
            Direction.SOUTHWEST,
        ):
            pos = ore_pos.add(direction)
            if not base.in_bounds(pos, self.width, self.height):
                continue
            if not ct.is_in_vision(pos):
                continue
            if ct.get_tile_env(pos) != Environment.EMPTY and not ct.is_tile_passable(pos):
                continue
            building_id = ct.get_tile_building_id(pos)
            if building_id is not None:
                entity_type = ct.get_entity_type(building_id)
                if entity_type not in (*base.WALKABLE_BUILDINGS, EntityType.MARKER):
                    continue
            return pos
        return None

    def plan_visible_ore(self, ct: Controller) -> tuple[Position | None, Position | None]:
        assert self.preferred_dir is not None
        current = ct.get_position()
        my_claim = self.claim_owner_token(ct)
        best_ore: Position | None = None
        best_stand: Position | None = None
        best_score = 10 ** 9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
                continue
            building_id = ct.get_tile_building_id(pos)
            if building_id is not None and ct.get_entity_type(building_id) not in (EntityType.MARKER,):
                continue
            claim_owner = self.recent_ore_claim_owner(ct, pos)
            if claim_owner is not None and claim_owner != my_claim and pos != self.target_ore:
                continue
            stand = self.find_build_stand(ct, pos)
            if stand is None:
                continue
            score = (
                current.distance_squared(stand) * 10
                + current.distance_squared(pos)
                + base.direction_rank(self.preferred_dir, current.direction_to(stand))
            )
            if claim_owner == my_claim:
                score -= 20
            if score < best_score:
                best_ore = pos
                best_stand = stand
                best_score = score
        return best_ore, best_stand

    def ore_target_still_valid(self, ct: Controller, pos: Position) -> bool:
        if not ct.is_in_vision(pos):
            return False
        if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is not None and ct.get_entity_type(building_id) not in (EntityType.MARKER,):
            return False
        claim_owner = self.recent_ore_claim_owner(ct, pos)
        if claim_owner is not None and claim_owner != self.claim_owner_token(ct):
            if ct.get_position().distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
                return False
        return True

    # -------------------------------------------------------------------
    # Assault logic
    # -------------------------------------------------------------------

    def run_assault(self, ct: Controller) -> None:
        assert self.enemy_guesses is not None
        current = ct.get_position()
        current_round = ct.get_current_round()
        self.absorb_guess_markers(ct)

        tile_building = ct.get_tile_building_id(current)
        if tile_building is not None and ct.get_team(tile_building) != ct.get_team():
            if ct.can_fire(current):
                ct.fire(current)
                return

        self.scan_for_enemy_core(ct)

        if self.enemy_core_pos is not None:
            if self.is_assault_engineer(ct):
                if self.try_maintain_ammo_chain(ct):
                    return
                if self.run_siege(ct):
                    return
            else:
                if self.primary_gunner_count(ct) < len(self.primary_gunner_positions()):
                    if self.run_siege(ct):
                        return
                elif current_round % 4 == (ct.get_id() % 4):
                    # Non-engineers only do lightweight, infrequent build checks near the core.
                    if self.run_siege(ct):
                        return

        target = self.enemy_core_pos or self.enemy_guesses[
            self.assault_guess_index % len(self.enemy_guesses)
        ]

        if self.enemy_core_pos is None and ct.is_in_vision(target):
            self.mark_guess_status(ct, self.assault_guess_index % len(self.enemy_guesses), confirmed=False)
            self.advance_assault_guess()
            target = self.enemy_guesses[self.assault_guess_index % len(self.enemy_guesses)]

        if self.run_assault_breakout(ct):
            self.stall_turns = 0
            return

        moved = self.advance_assault_flow(ct, target)
        if not moved:
            moved = self._assault_step_toward(ct, target)

        if moved:
            self.stall_turns = 0
            return

        self.stall_turns += 1
        if (
            self.core_pos is not None
            and current.distance_squared(self.core_pos) >= 49
            and self.stall_turns >= base.STALL_FOR_LAUNCHER
            and self.try_build_path_launcher(ct)
        ):
            self.stall_turns = 0

    def is_assault_engineer(self, ct: Controller) -> bool:
        if self.ammo_gunner_pos is not None:
            return True
        return (ct.get_id() + (self.assigned_guess_index or 0)) % 4 == 0

    def advance_assault_flow(self, ct: Controller, target: Position) -> bool:
        current = ct.get_position()
        if self.enemy_core_pos is not None or current.distance_squared(target) <= 144:
            if self.enemy_core_pos is not None:
                open_slots = [
                    pos for pos in self.primary_gunner_positions()
                    if base.building_type_at(ct, pos) != EntityType.GUNNER
                ]
                if open_slots:
                    target = min(open_slots, key=lambda pos: current.distance_squared(pos))
            return self._assault_step_toward(ct, target)
        return base.Player.advance_assault_flow(self, ct, target)

    def absorb_guess_markers(self, ct: Controller) -> None:
        if self.enemy_guesses is None:
            return
        for entity_id in ct.get_nearby_buildings():
            if ct.get_entity_type(entity_id) != EntityType.MARKER:
                continue
            if ct.get_team(entity_id) != ct.get_team():
                continue
            status = decode_guess_status(ct.get_marker_value(entity_id))
            if status is None:
                continue
            guess_index, confirmed = status
            if guess_index >= len(self.enemy_guesses):
                continue
            if confirmed:
                self.confirmed_guess_index = guess_index
                self.dead_guess_indices.discard(guess_index)
                self.assault_guess_index = guess_index
            elif self.confirmed_guess_index != guess_index:
                self.dead_guess_indices.add(guess_index)

    def place_local_marker(self, ct: Controller, value: int, preferred: Position | None = None) -> None:
        candidates = []
        if preferred is not None:
            candidates.append(preferred)
        candidates.append(ct.get_position())
        candidates.extend(ct.get_position().add(direction) for direction in Direction)
        seen: set[tuple[int, int]] = set()
        for pos in candidates:
            key = (pos.x, pos.y)
            if key in seen:
                continue
            seen.add(key)
            if self.width is not None and self.height is not None and not base.in_bounds(pos, self.width, self.height):
                continue
            if ct.can_place_marker(pos):
                ct.place_marker(pos, value)
                return

    def mark_guess_status(self, ct: Controller, guess_index: int, confirmed: bool) -> None:
        if self.enemy_guesses is None or guess_index >= len(self.enemy_guesses):
            return
        self.place_local_marker(
            ct,
            encode_guess_status(guess_index, confirmed),
            preferred=self.enemy_guesses[guess_index],
        )
        if confirmed:
            self.confirmed_guess_index = guess_index
            self.dead_guess_indices.discard(guess_index)
            self.assault_guess_index = guess_index
        else:
            self.dead_guess_indices.add(guess_index)

    def advance_assault_guess(self) -> None:
        assert self.enemy_guesses is not None
        if not self.enemy_guesses:
            return
        if self.confirmed_guess_index is not None:
            self.assault_guess_index = self.confirmed_guess_index
            self.assault_flow_signature = None
            self.assault_flow = None
            return
        active = [i for i in range(len(self.enemy_guesses)) if i not in self.dead_guess_indices]
        if not active:
            active = list(range(len(self.enemy_guesses)))
        current = self.assault_guess_index % len(self.enemy_guesses)
        if current in active:
            next_index = active[(active.index(current) + 1) % len(active)]
        else:
            next_index = active[0]
        self.assault_guess_index = next_index
        self.assault_flow_signature = None
        self.assault_flow = None

    def scan_for_enemy_core(self, ct: Controller) -> None:
        for entity_id in ct.get_nearby_buildings():
            if ct.get_entity_type(entity_id) != EntityType.CORE:
                continue
            if ct.get_team(entity_id) == ct.get_team():
                continue
            self.enemy_core_pos = ct.get_position(entity_id)
            if self.enemy_guesses is not None:
                best_index = min(
                    range(len(self.enemy_guesses)),
                    key=lambda i: self.enemy_guesses[i].distance_squared(self.enemy_core_pos),
                )
                self.mark_guess_status(ct, best_index, confirmed=True)
            return

    # -------------------------------------------------------------------
    # Siege layout: fed corner gunners first, barriers tucked between them
    # and the core so the rays stay clean.
    # -------------------------------------------------------------------

    def primary_gunner_positions(self) -> list[Position]:
        assert self.enemy_core_pos is not None
        core = self.enemy_core_pos
        positions = [
            Position(core.x - 2, core.y - 2),
            Position(core.x + 2, core.y - 2),
            Position(core.x - 2, core.y + 2),
            Position(core.x + 2, core.y + 2),
        ]
        if self.width is None or self.height is None:
            return positions
        return [pos for pos in positions if base.in_bounds(pos, self.width, self.height)]

    def secondary_gunner_positions(self) -> list[Position]:
        assert self.enemy_core_pos is not None
        core = self.enemy_core_pos
        positions = [
            Position(core.x, core.y - 3),
            Position(core.x + 3, core.y),
            Position(core.x, core.y + 3),
            Position(core.x - 3, core.y),
        ]
        if self.width is None or self.height is None:
            return positions
        return [pos for pos in positions if base.in_bounds(pos, self.width, self.height)]

    def barrier_positions_for_gunner(self, gunner_pos: Position) -> list[Position]:
        assert self.enemy_core_pos is not None
        dx = 1 if gunner_pos.x < self.enemy_core_pos.x else -1
        dy = 1 if gunner_pos.y < self.enemy_core_pos.y else -1
        return [
            Position(gunner_pos.x + dx, gunner_pos.y),
            Position(gunner_pos.x, gunner_pos.y + dy),
        ]

    def siege_positions(self) -> tuple[list[Position], list[Position]]:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None

        barriers: list[Position] = []
        for gunner_pos in self.primary_gunner_positions():
            for pos in self.barrier_positions_for_gunner(gunner_pos):
                if base.in_bounds(pos, self.width, self.height) and not base.on_core_tile(pos, self.enemy_core_pos):
                    if pos not in barriers:
                        barriers.append(pos)

        gunners: list[Position] = []
        for pos in [*self.primary_gunner_positions(), *self.secondary_gunner_positions()]:
            if not base.in_bounds(pos, self.width, self.height):
                continue
            if base.on_core_tile(pos, self.enemy_core_pos):
                continue
            if not self.gunner_slot_hits_core(pos):
                continue
            if pos not in gunners:
                gunners.append(pos)

        return barriers, gunners

    def gunner_slot_hits_core(self, pos: Position) -> bool:
        assert self.enemy_core_pos is not None
        face = pos.direction_to(self.enemy_core_pos)
        if face == Direction.CENTRE:
            return False
        cursor = pos
        for _ in range(4):
            cursor = cursor.add(face)
            if base.on_core_tile(cursor, self.enemy_core_pos):
                return True
        return False

    def primary_gunner_count(self, ct: Controller) -> int:
        count = 0
        for pos in self.primary_gunner_positions():
            if base.building_type_at(ct, pos) == EntityType.GUNNER:
                count += 1
        return count

    def run_siege(self, ct: Controller) -> bool:
        assert self.enemy_core_pos is not None
        current = ct.get_position()
        barrier_tiles, gunner_tiles = self.siege_positions()
        primary_tiles = [pos for pos in self.primary_gunner_positions() if pos in gunner_tiles]

        build_targets = list(primary_tiles)
        if self.primary_gunner_count(ct) >= len(primary_tiles) and base.titanium_surplus_for_assault(ct):
            build_targets = gunner_tiles

        for pos in sorted(build_targets, key=lambda p: current.distance_squared(p)):
            if self.try_build_gunner_slot(ct, pos):
                return True

        for pos in sorted(barrier_tiles, key=lambda p: current.distance_squared(p)):
            if self.try_build_barrier_slot(ct, pos):
                return True

        goals = [
            pos for pos in build_targets
            if base.building_type_at(ct, pos) != EntityType.GUNNER
        ]
        if not goals:
            goals = [
                pos for pos in barrier_tiles
                if base.building_type_at(ct, pos) != EntityType.BARRIER
            ]
        if goals:
            target = min(goals, key=lambda p: current.distance_squared(p))
            return self._assault_step_toward(ct, target)
        return False

    def try_build_gunner_slot(self, ct: Controller, pos: Position) -> bool:
        assert self.enemy_core_pos is not None
        face = pos.direction_to(self.enemy_core_pos)
        building_id = ct.get_tile_building_id(pos) if ct.is_in_vision(pos) else None
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.GUNNER and ct.get_team(building_id) == ct.get_team():
                return False
            if entity_type == EntityType.MARKER:
                pass
            elif entity_type in base.WALKABLE_BUILDINGS and ct.get_team(building_id) == ct.get_team():
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return True
                return False
            else:
                return False

        cost = ct.get_gunner_cost()
        if not base.can_afford(ct, cost[0], cost[1], reserve=base.reserve_assault(ct)):
            return False
        if ct.can_build_gunner(pos, face):
            ct.build_gunner(pos, face)
            return True
        return False

    def try_build_barrier_slot(self, ct: Controller, pos: Position) -> bool:
        building_id = ct.get_tile_building_id(pos) if ct.is_in_vision(pos) else None
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.BARRIER and ct.get_team(building_id) == ct.get_team():
                return False
            if entity_type == EntityType.MARKER:
                pass
            elif entity_type in base.WALKABLE_BUILDINGS and ct.get_team(building_id) == ct.get_team():
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return True
                return False
            else:
                return False

        cost = ct.get_barrier_cost()
        if not base.can_afford(ct, cost[0], cost[1], reserve=base.reserve_assault(ct)):
            return False
        if ct.can_build_barrier(pos):
            ct.build_barrier(pos)
            return True
        return False

    # -------------------------------------------------------------------
    # Ammo chains
    # -------------------------------------------------------------------

    def reset_ammo_project(self) -> None:
        self.ammo_gunner_pos = None
        self.ammo_ore_pos = None
        self.ammo_stand_pos = None
        self.ammo_feed_pos = None
        self.ammo_path = []
        self.ammo_chain_done = False

    def try_maintain_ammo_chain(self, ct: Controller) -> bool:
        assert self.enemy_core_pos is not None
        if not self.ammo_project_valid(ct):
            if ct.get_current_round() - self.last_ammo_replan_round < 6:
                return False
            self.last_ammo_replan_round = ct.get_current_round()
            self.reset_ammo_project()
            if not self.select_ammo_project(ct):
                return False

        assert self.ammo_gunner_pos is not None
        assert self.ammo_ore_pos is not None
        assert self.ammo_path

        current = ct.get_position()
        ore_building = (
            ct.get_tile_building_id(self.ammo_ore_pos)
            if ct.is_in_vision(self.ammo_ore_pos)
            else None
        )
        self.refresh_ore_claim(ct, self.ammo_ore_pos)
        if ore_building is None or ct.get_entity_type(ore_building) != EntityType.HARVESTER:
            stand = self.ammo_stand_pos or self.find_build_stand(ct, self.ammo_ore_pos)
            if stand is None:
                self.reset_ammo_project()
                return False
            self.ammo_stand_pos = stand
            if (
                current.distance_squared(self.ammo_ore_pos) <= GameConstants.ACTION_RADIUS_SQ
                and abs(current.x - self.ammo_ore_pos.x) + abs(current.y - self.ammo_ore_pos.y) == 1
            ):
                cost = ct.get_harvester_cost()
                if base.can_afford(ct, cost[0], cost[1], reserve=base.reserve_assault(ct)) and ct.can_build_harvester(self.ammo_ore_pos):
                    ct.build_harvester(self.ammo_ore_pos)
                    return True
                return False
            return self._assault_step_toward(ct, stand)

        for index, pos in enumerate(self.ammo_path):
            dest = self.ammo_gunner_pos if index == len(self.ammo_path) - 1 else self.ammo_path[index + 1]
            if self.transport_link_ready(ct, pos, dest):
                continue
            if current.distance_squared(pos) > GameConstants.ACTION_RADIUS_SQ:
                return self._assault_step_toward(ct, pos)
            if self.ensure_transport_link(ct, pos, dest):
                return True
            if current != pos:
                return self._assault_step_toward(ct, pos)
            return False

        self.ammo_chain_done = True
        return False

    def ammo_project_valid(self, ct: Controller) -> bool:
        if (
            self.ammo_gunner_pos is None
            or self.ammo_ore_pos is None
            or self.ammo_feed_pos is None
            or not self.ammo_path
        ):
            return False
        if self.enemy_core_pos is None:
            return False
        gunner_id = ct.get_tile_building_id(self.ammo_gunner_pos) if ct.is_in_vision(self.ammo_gunner_pos) else None
        if gunner_id is None or ct.get_entity_type(gunner_id) != EntityType.GUNNER or ct.get_team(gunner_id) != ct.get_team():
            return False
        if not self.gunner_needs_feed(ct, self.ammo_gunner_pos):
            return False
        if not ct.is_in_vision(self.ammo_ore_pos):
            return False
        ore_id = ct.get_tile_building_id(self.ammo_ore_pos) if ct.is_in_vision(self.ammo_ore_pos) else None
        if ore_id is None:
            return False
        ore_type = ct.get_entity_type(ore_id)
        return ore_type in (EntityType.HARVESTER, EntityType.MARKER)

    def select_ammo_project(self, ct: Controller) -> bool:
        assert self.enemy_core_pos is not None
        current = ct.get_position()
        best_score = 10 ** 9
        best_project: tuple[Position, Position, Position, Position, list[Position]] | None = None

        gunner_positions = [
            pos for pos in self.siege_positions()[1]
            if base.building_type_at(ct, pos) == EntityType.GUNNER and self.gunner_needs_feed(ct, pos)
        ]
        gunner_positions.sort(key=lambda pos: (0 if pos in self.primary_gunner_positions() else 1, current.distance_squared(pos)))

        for gunner_pos in gunner_positions:
            gunner_id = ct.get_tile_building_id(gunner_pos) if ct.is_in_vision(gunner_pos) else None
            if gunner_id is None:
                continue
            feed_sides = self.feed_side_candidates(ct, gunner_pos, gunner_id)
            if not feed_sides:
                continue
            for ore_pos in ct.get_nearby_tiles():
                if ct.get_tile_env(ore_pos) != Environment.ORE_TITANIUM:
                    continue
                if ore_pos.distance_squared(gunner_pos) > 81:
                    continue
                claim_owner = self.recent_ore_claim_owner(ct, ore_pos)
                if claim_owner is not None and claim_owner != self.claim_owner_token(ct):
                    continue
                ore_building = ct.get_tile_building_id(ore_pos)
                if ore_building is not None and ct.get_entity_type(ore_building) not in (EntityType.HARVESTER, EntityType.MARKER):
                    continue
                for stand in self.ore_start_positions(ct, ore_pos):
                    for feed_pos in feed_sides:
                        path = self.find_transport_path(ct, stand, feed_pos)
                        if not path:
                            continue
                        score = (
                            len(path) * 4
                            + current.distance_squared(stand)
                            + gunner_pos.distance_squared(self.enemy_core_pos)
                            - (6 if ore_building is not None and ct.get_entity_type(ore_building) == EntityType.HARVESTER else 0)
                        )
                        if score < best_score:
                            best_score = score
                            best_project = (gunner_pos, ore_pos, stand, feed_pos, path)

        if best_project is None:
            return False

        (
            self.ammo_gunner_pos,
            self.ammo_ore_pos,
            self.ammo_stand_pos,
            self.ammo_feed_pos,
            self.ammo_path,
        ) = best_project
        self.ammo_chain_done = False
        return True

    def gunner_needs_feed(self, ct: Controller, gunner_pos: Position) -> bool:
        if self.enemy_core_pos is None or self.width is None or self.height is None:
            return False
        gunner_id = ct.get_tile_building_id(gunner_pos) if ct.is_in_vision(gunner_pos) else None
        if gunner_id is None or ct.get_entity_type(gunner_id) != EntityType.GUNNER:
            return False
        if ct.get_team(gunner_id) != ct.get_team():
            return False

        for adj in base.adjacent_cardinals(gunner_pos):
            if not base.in_bounds(adj, self.width, self.height):
                continue
            building_id = ct.get_tile_building_id(adj) if ct.is_in_vision(adj) else None
            if building_id is None:
                continue
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.CONVEYOR and adj.add(ct.get_direction(building_id)) == gunner_pos:
                return False
            if entity_type == EntityType.BRIDGE and ct.get_bridge_target(building_id) == gunner_pos:
                return False
        return True

    def feed_side_candidates(self, ct: Controller, gunner_pos: Position, gunner_id: int) -> list[Position]:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        face = ct.get_direction(gunner_id)
        options: list[Position] = []
        for adj in base.adjacent_cardinals(gunner_pos):
            if not base.in_bounds(adj, self.width, self.height):
                continue
            if base.on_core_tile(adj, self.enemy_core_pos):
                continue
            if face in base.CARDINAL_DIRECTIONS and adj.direction_to(gunner_pos) == face:
                continue
            if not self.transport_path_tile_open(ct, adj):
                continue
            options.append(adj)
        return options

    def ore_start_positions(self, ct: Controller, ore_pos: Position) -> list[Position]:
        starts: list[Position] = []
        for direction in base.CARDINAL_DIRECTIONS:
            pos = ore_pos.add(direction)
            if self.transport_path_tile_open(ct, pos):
                starts.append(pos)
        starts.sort(key=lambda pos: pos.distance_squared(ore_pos))
        return starts

    def transport_path_tile_open(self, ct: Controller, pos: Position) -> bool:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        if not base.in_bounds(pos, self.width, self.height):
            return False
        if not ct.is_in_vision(pos):
            return False
        if base.on_core_tile(pos, self.enemy_core_pos):
            return False
        if ct.get_tile_env(pos) != Environment.EMPTY and not ct.is_tile_passable(pos):
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is None:
            return True
        entity_type = ct.get_entity_type(building_id)
        if entity_type == EntityType.MARKER:
            return True
        if ct.get_team(building_id) != ct.get_team():
            return True
        return entity_type in base.WALKABLE_BUILDINGS or entity_type in base.ROUTABLE_ENDPOINTS

    def ensure_transport_link(self, ct: Controller, pos: Position, dest: Position) -> bool:
        is_diagonal = pos.x != dest.x and pos.y != dest.y
        is_far = pos.distance_squared(dest) > 1
        use_bridge = is_diagonal or is_far

        if use_bridge and pos.distance_squared(dest) > 9:
            return False

        building_id = ct.get_tile_building_id(pos)
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if use_bridge and entity_type == EntityType.BRIDGE and ct.get_bridge_target(building_id) == dest:
                return True
            if (
                not use_bridge
                and entity_type == EntityType.CONVEYOR
                and ct.get_direction(building_id) == pos.direction_to(dest)
            ):
                return True
            if entity_type == EntityType.MARKER:
                pass
            elif ct.get_team(building_id) != ct.get_team() or entity_type in base.WALKABLE_BUILDINGS:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return False
                return False
            else:
                return False

        if use_bridge:
            cost = ct.get_bridge_cost()
            if not base.can_afford(ct, cost[0], cost[1], reserve=base.BASE_RESERVE):
                return False
            if ct.can_build_bridge(pos, dest):
                ct.build_bridge(pos, dest)
                return True
            return False

        cost = ct.get_conveyor_cost()
        if not base.can_afford(ct, cost[0], cost[1], reserve=base.BASE_RESERVE):
            return False
        if ct.can_build_conveyor(pos, pos.direction_to(dest)):
            ct.build_conveyor(pos, pos.direction_to(dest))
            return True
        return False

    def find_transport_path(self, ct: Controller, start: Position, goal: Position) -> list[Position] | None:
        if start == goal:
            return [start]
        if not self.transport_path_tile_open(ct, start) or not self.transport_path_tile_open(ct, goal):
            return None

        queue: deque[Position] = deque([start])
        previous: dict[tuple[int, int], Position | None] = {(start.x, start.y): None}
        while queue:
            pos = queue.popleft()
            for direction in base.CARDINAL_DIRECTIONS:
                nxt = pos.add(direction)
                key = (nxt.x, nxt.y)
                if key in previous or not self.transport_path_tile_open(ct, nxt):
                    continue
                previous[key] = pos
                if nxt == goal:
                    path = [goal]
                    cursor = pos
                    while cursor is not None:
                        path.append(cursor)
                        cursor = previous[(cursor.x, cursor.y)]
                    path.reverse()
                    return path
                queue.append(nxt)
        return None

    def transport_link_ready(self, ct: Controller, pos: Position, dest: Position) -> bool:
        if not ct.is_in_vision(pos):
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is None:
            return False
        entity_type = ct.get_entity_type(building_id)
        if entity_type == EntityType.CONVEYOR:
            return ct.get_direction(building_id) == pos.direction_to(dest)
        if entity_type == EntityType.BRIDGE:
            return ct.get_bridge_target(building_id) == dest
        return False