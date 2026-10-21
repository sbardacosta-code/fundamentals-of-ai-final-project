"""One command for the six-stage, 100-review pilot; warm replay is the default."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from . import pilot, pilot_memo
from .storage import atomic_json


def run_workflow(input_path='data/source/cost_100.csv', directory='artifacts/pilot',
                 phase='warm', execute_paid=False):
    if phase not in ('cold', 'warm'):
        raise ValueError('Unknown pilot phase')
    if phase == 'cold' and not execute_paid:
        raise ValueError('Cold execution requires --execute-paid')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # Preserve earlier warm observations; this invocation is a new measurement.
    metadata_path = root / (phase + '.json')
    if phase == 'warm' and metadata_path.exists():
        history = root / 'history'
        history.mkdir(exist_ok=True)
        metadata_path.replace(history / ('warm-' + uuid.uuid4().hex + '.json'))
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    pilot.run_pilot(input_path, directory, phase)
    metadata = json.loads(metadata_path.read_text())
    metadata['phase_started_at'] = started_at
    atomic_json(metadata_path, metadata)
    result = pilot_memo.run_memo(directory, phase=phase, execute_paid=execute_paid)
    elapsed = time.monotonic() - started
    result['workflow_invocation_seconds'] = elapsed
    result['workflow_invocation_started_at'] = started_at
    if phase == 'warm':
        result.update(elapsed_seconds=elapsed, active_elapsed_seconds=elapsed,
                      wall_elapsed_seconds=elapsed, end_to_end_wall_seconds=elapsed,
                      pause_seconds=0, runtime_basis='single_workflow_wall_clock')
    atomic_json(metadata_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default='data/source/cost_100.csv')
    parser.add_argument('--directory', default='artifacts/pilot')
    parser.add_argument('--phase', choices=['cold', 'warm'], default='warm')
    parser.add_argument('--execute-paid', action='store_true')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    if args.phase == 'cold' and not args.execute_paid:
        parser.error('Cold execution requires --execute-paid')
    if args.execute_paid:
        pilot.load_api_key(args.env_file)
        pilot_memo.load_api_key(args.env_file)
    result = run_workflow(args.input, args.directory, args.phase, args.execute_paid)
    print(json.dumps({key: result[key] for key in (
        'phase', 'status_counts', 'stages_completed', 'full_pilot_complete',
        'workflow_invocation_seconds', 'ledger')}, indent=2))


if __name__ == '__main__':
    main()
