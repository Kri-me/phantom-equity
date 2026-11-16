from datetime import datetime
from pathlib import Path
import subprocess
import sys


# ============================================================
# CONFIGURATION
# ============================================================

SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = SCRIPTS_DIR.parent / "outputs"

PIPELINE_STEPS = [
    "signals.py",
    "transfer_signals.py",
    "etl_load.py",
    "case_builder.py",
    "build_entity_network.py",
    "corroboration_engine.py",
]


# ============================================================
# LOGGING
# ============================================================

def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    run_started = datetime.now()
    log_name = (
        f"pipeline_run_{run_started.strftime('%Y%m%d_%H%M%S')}.txt"
    )
    log_path = OUTPUTS_DIR / "pipeline log" / log_name
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Pipeline started: {timestamp()}")
    print(f"Scripts directory: {SCRIPTS_DIR}")
    print(f"Log file: {log_path}")
    print()

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Pipeline started: {timestamp()}\n")
        log.write(f"Scripts directory: {SCRIPTS_DIR}\n")
        log.write(f"Execution order: {' -> '.join(PIPELINE_STEPS)}\n\n")

        for step_number, script_name in enumerate(
            PIPELINE_STEPS,
            start=1,
        ):
            script_path = SCRIPTS_DIR / script_name

            log.write("=" * 80 + "\n")
            log.write(
                f"STEP {step_number}/{len(PIPELINE_STEPS)}: "
                f"{script_name}\n"
            )
            log.write(f"Started: {timestamp()}\n")
            log.flush()

            print(
                f"[{step_number}/{len(PIPELINE_STEPS)}] "
                f"Running {script_name}..."
            )

            if not script_path.is_file():
                message = f"Required pipeline script not found: {script_path}"

                log.write(message + "\n")
                log.write(f"Failed: {timestamp()}\n")
                print(f"ERROR: {message}")
                print(f"Pipeline stopped. See log: {log_path}")
                return 1

            try:
                result = subprocess.run(
                    [sys.executable, str(script_path)],
                    cwd=str(SCRIPTS_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )

                if result.stdout:
                    print(result.stdout, end="")

                    log.write(result.stdout)

                    if not result.stdout.endswith("\n"):
                        log.write("\n")

                log.write(f"\nExit code: {result.returncode}\n")
                log.write(f"Finished: {timestamp()}\n")
                log.flush()

                if result.returncode != 0:
                    message = (
                        f"Pipeline stopped: {script_name} "
                        f"exited with code {result.returncode}."
                    )

                    log.write(message + "\n")
                    print(message)
                    print(f"See log: {log_path}")
                    return result.returncode

                print(f"Completed: {script_name}\n")

            except OSError as exc:
                message = (
                    f"Could not execute {script_name}: "
                    f"{type(exc).__name__}: {exc}"
                )

                log.write(message + "\n")
                log.write(f"Failed: {timestamp()}\n")
                print(f"ERROR: {message}")
                print(f"Pipeline stopped. See log: {log_path}")
                return 1

        elapsed = datetime.now() - run_started

        log.write("=" * 80 + "\n")
        log.write(f"Pipeline completed successfully: {timestamp()}\n")
        log.write(f"Total elapsed time: {elapsed}\n")

    print("=" * 80)
    print("PIPELINE COMPLETED SUCCESSFULLY")
    print(f"Elapsed time: {elapsed}")
    print(f"Log: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())