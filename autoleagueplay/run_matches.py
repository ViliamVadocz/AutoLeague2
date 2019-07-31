import random
import time
from pathlib import Path
from typing import Mapping

from rlbot.matchconfig.conversions import read_match_config_from_file
from rlbot.matchconfig.match_config import MatchConfig, PlayerConfig, Team
from rlbot.parsing.bot_config_bundle import BotConfigBundle
from rlbot.training.training import Fail
from rlbot.utils.logging_utils import get_logger
from rlbottraining.exercise_runner import run_playlist

from autoleagueplay.generate_matches import generate_round_robin_matches
from autoleagueplay.ladder import Ladder
from autoleagueplay.match_exercise import MatchExercise, MatchGrader
from autoleagueplay.match_result import CombinedScore, MatchResult
from autoleagueplay.paths import WorkingDir, PackageFiles
from autoleagueplay.replays import ReplayPreference, ReplayMonitor

logger = get_logger('autoleagueplay')


def make_match_config(working_dir: WorkingDir, blue: BotConfigBundle, orange: BotConfigBundle) -> MatchConfig:
    match_config = read_match_config_from_file(PackageFiles.default_match_config)
    match_config.game_map = random.choice([
        'ChampionsField',
        'Farmstead',
        'DFHStadium',
        'Wasteland',
        'BeckwithPark'
    ])
    blue_path = str((Path(blue.config_directory) / blue.config_file_name).relative_to(working_dir.bots))
    orange_path = str((Path(orange.config_directory) / orange.config_file_name).relative_to(working_dir.bots))
    match_config.player_configs = [
        PlayerConfig.bot_config(working_dir.bots / blue_path, Team.BLUE),
        PlayerConfig.bot_config(working_dir.bots / orange_path, Team.ORANGE),
    ]
    return match_config


def run_league_play(working_dir: WorkingDir, odd_week: bool, replay_preference: ReplayPreference):
    """
    Progress League Play by playing the next event, round robin, or match as specified by the run_option.
    """

    # Load all bots
    bots = working_dir.get_bots()
    if len(bots) < 2:
        # Not enough bots to play
        print(f'Not enough bots to run league play. Must have at least 2 (found {len(bots)})')
        return
    else:
        print(f'Loaded {len(bots)} bots')

    ladder = Ladder.read(working_dir.ladder)

    # We need the result of every match to create the next ladder. For each match in each round robin, if a result
    # exist already, it will be parsed, if it doesn't exist, it will be played.
    # When all results have been found, the new ladder can be completed and saved.
    new_ladder = Ladder(ladder.bots)
    event_results = []

    # playing_division_indices contains either even or odd indices.
    # If there is only one division always play that division (division 0, quantum).
    playing_division_indices = range(ladder.division_count())[int(odd_week) % 2::2] if ladder.division_count() > 1 else [0]

    # The divisions play in reverse order, so quantum/overclocked division plays last
    for div_index in playing_division_indices[::-1]:
        print(f'Starting round robin for the {Ladder.DIVISION_NAMES[div_index]} division')

        rr_bots = ladder.round_robin_participants(div_index)
        rr_matches = generate_round_robin_matches(rr_bots)
        rr_results = []

        for match_participants in rr_matches:

            # Check if match has already been play, i.e. the result file already exist
            result_path = working_dir.get_match_result(div_index, match_participants[0], match_participants[1])
            if result_path.exists():
                # Found existing result
                try:
                    print(f'Found existing result {result_path.name}')
                    result = MatchResult.read(result_path)

                    rr_results.append(result)

                except Exception as e:
                    print(f'Error loading result {result_path.name}. Fix/delete the result and run script again.')
                    raise e

            else:
                assert match_participants[0] in bots, f'{match_participants[0]} was not found in \'{working_dir.bots}\''
                assert match_participants[1] in bots, f'{match_participants[1]} was not found in \'{working_dir.bots}\''

                # Play the match
                print(f'Starting match: {match_participants[0]} vs {match_participants[1]}. Waiting for match to finish...')
                match_config = make_match_config(working_dir, bots[match_participants[0]], bots[match_participants[1]])
                match = MatchExercise(
                    name=f'{match_participants[0]} vs {match_participants[1]}',
                    match_config=match_config,
                    grader=MatchGrader(
                        replay_monitor=ReplayMonitor(replay_preference=replay_preference),
                    )
                )

                # For loop, but should only run exactly once
                for exercise_result in run_playlist([match]):

                    # Warn users if no replay was found
                    if isinstance(exercise_result.grade, Fail) and exercise_result.exercise.grader.replay_monitor.replay_id == None:
                        print(f'WARNING: No replay was found for the match \'{match_participants[0]} vs {match_participants[1]}\'. Is Bakkesmod injected and \'Automatically save all replays\' enabled?')

                    # Save result in file
                    result = exercise_result.exercise.grader.match_result
                    result.write(result_path)
                    print(f'Match finished {result.blue_goals}-{result.orange_goals}. Saved result as {result_path}')

                    rr_results.append(result)

                    # Let the winner celebrate and the scoreboard time enough to appear for a few seconds.
                    # This sleep not required unnecessary.
                    time.sleep(8)

        print(f'{Ladder.DIVISION_NAMES[div_index]} division done')
        event_results.append(rr_results)

        # Find bots' overall score for the round robin
        overall_scores = [CombinedScore.calc_score(bot, rr_results) for bot in rr_bots]
        sorted_overall_scores = sorted(overall_scores)[::-1]
        print(f'Bots\' overall performance in {Ladder.DIVISION_NAMES[div_index]} division:')
        for score in sorted_overall_scores:
            print(f'> {score.bot}: goal_diff={score.goal_diff}, goals={score.goals}, shots={score.shots}, saves={score.saves}, points={score.points}')

        # Rearrange bots in division on the new ladder
        first_bot_index = new_ladder.division_size * div_index
        bots_to_rearrange = len(rr_bots)
        for i in range(bots_to_rearrange):
            new_ladder.bots[first_bot_index + i] = sorted_overall_scores[i].bot

    # Save new ladder
    Ladder.write(new_ladder, working_dir.new_ladder)
    print(f'Done. Saved new ladder as {working_dir.new_ladder.name}')

    return new_ladder