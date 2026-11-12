# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "rich>=13.7.0",
# ]
# ///

import argparse
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Cross-platform single-key reader for TUI navigation
if os.name == "nt":
    import msvcrt

    def get_key() -> str:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            return {b"H": "UP", b"P": "DOWN"}.get(ch2, "")
        if ch == b" ":
            return "SPACE"
        if ch in (b"\r", b"\n"):
            return "ENTER"
        if ch in (b"\x1b", b"q", b"Q"):
            return "QUIT"
        if ch in (b"a", b"A"):
            return "ALL"
        return ""
else:
    import termios
    import tty

    def get_key() -> str:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    return {"A": "UP", "B": "DOWN"}.get(ch3, "")
                return "QUIT"
            if ch == " ":
                return "SPACE"
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch in ("q", "Q", "\x03"):
                return "QUIT"
            if ch in ("a", "A"):
                return "ALL"
            if ch == "k":
                return "UP"
            if ch == "j":
                return "DOWN"
            return ""
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


EDITION_PATTERN = re.compile(r'^(\s*edition\s*=\s*)["\'](\d{4})["\']', re.MULTILINE)
console = Console()


class ScanStatus(Enum):
    READY = "READY"
    DIRTY = "DIRTY GIT"
    SKIP_EDITION = "UP TO DATE"
    SKIP_WORKSPACE = "WORKSPACE"
    NO_GIT = "NO GIT"


@dataclass
class ProjectInfo:
    path: Path
    name: str
    current_edition: str
    scan_status: ScanStatus
    selected: bool
    run_state: str = "Pending"
    run_color: str = "dim"
    error_detail: str = ""


def get_current_edition(content: str) -> str:
    """Extracts the current edition from Cargo.toml content. Defaults to '2015'."""
    match = EDITION_PATTERN.search(content)
    return match.group(2) if match else "2015"


def check_git_status(project_dir: Path) -> str:
    """Returns 'CLEAN', 'DIRTY', or 'NO_GIT'."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "NO_GIT"
    return "DIRTY" if result.stdout.strip() else "CLEAN"


def scan_projects(root_dir: Path, target_edition: str) -> list[ProjectInfo]:
    """Stage 1: Recursively scans projects and evaluates their edition and Git status."""
    ignore_dirs = {"target", ".git", ".cargo", "node_modules"}
    projects: list[ProjectInfo] = []

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        if "Cargo.toml" not in files:
            continue

        p_dir = Path(root)
        cargo_toml = p_dir / "Cargo.toml"
        try:
            content = cargo_toml.read_text(encoding="utf-8")
        except Exception:
            continue

        if "[package]" not in content:
            projects.append(
                ProjectInfo(
                    path=p_dir,
                    name=p_dir.name,
                    current_edition="-",
                    scan_status=ScanStatus.SKIP_WORKSPACE,
                    selected=False,
                )
            )
            continue

        curr_ed = get_current_edition(content)
        if curr_ed == target_edition:
            projects.append(
                ProjectInfo(
                    path=p_dir,
                    name=p_dir.name,
                    current_edition=curr_ed,
                    scan_status=ScanStatus.SKIP_EDITION,
                    selected=False,
                )
            )
            continue

        git_state = check_git_status(p_dir)
        if git_state == "CLEAN":
            status = ScanStatus.READY
            selected = True
        elif git_state == "DIRTY":
            status = ScanStatus.DIRTY
            selected = False
        else:
            status = ScanStatus.NO_GIT
            selected = True

        projects.append(
            ProjectInfo(
                path=p_dir,
                name=p_dir.name,
                current_edition=curr_ed,
                scan_status=status,
                selected=selected,
            )
        )

    order = {
        ScanStatus.READY: 0,
        ScanStatus.NO_GIT: 1,
        ScanStatus.DIRTY: 2,
        ScanStatus.SKIP_EDITION: 3,
        ScanStatus.SKIP_WORKSPACE: 4,
    }
    projects.sort(key=lambda p: (order[p.scan_status], str(p.path)))
    return projects


def render_selection_table(
    projects: list[ProjectInfo],
    cursor: int,
    root_dir: Path,
    target_edition: str,
    branch: str | None,
) -> Panel:
    """Renders the Stage 2 interactive project selection table."""
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("Sel", width=5, justify="center")
    table.add_column("Project", style="bold")
    table.add_column("Edition", width=12, justify="center")
    table.add_column("Status", width=16, justify="center")
    table.add_column("Relative Path", style="dim")

    status_styles = {
        ScanStatus.READY: "bold green",
        ScanStatus.NO_GIT: "cyan",
        ScanStatus.DIRTY: "bold yellow",
        ScanStatus.SKIP_EDITION: "dim white",
        ScanStatus.SKIP_WORKSPACE: "dim white",
    }

    for idx, proj in enumerate(projects):
        is_cursor = idx == cursor
        row_style = "on grey23" if is_cursor else ""
        pointer = ">" if is_cursor else " "
        checkbox = "[green][x][/green]" if proj.selected else "[dim][ ][/dim]"

        st_style = status_styles[proj.scan_status]
        status_text = Text(proj.scan_status.value, style=st_style)

        ed_text = (
            f"{proj.current_edition} -> {target_edition}"
            if proj.current_edition not in ("-", target_edition)
            else proj.current_edition
        )
        rel_path = (
            str(proj.path.relative_to(root_dir)) if proj.path != root_dir else "."
        )

        table.add_row(
            f"{pointer}{checkbox}",
            proj.name,
            ed_text,
            status_text,
            rel_path,
            style=row_style,
        )

    selected_count = sum(1 for p in projects if p.selected)
    branch_info = (
        f" | Git branch: [bold magenta]{branch}[/bold magenta]" if branch else ""
    )
    subtitle = (
        f"[bold]Controls:[/bold] [cyan]↑/↓[/cyan] Move  "
        f"[cyan]Space[/cyan] Toggle  [cyan]a[/cyan] Toggle All  "
        f"[green]Enter[/green] Run ({selected_count} selected)  [red]q[/red] Quit"
    )
    return Panel(
        table,
        title=f"[bold]Stage 1 & 2: Select Projects to Migrate (Target: {target_edition})[/bold]{branch_info}",
        subtitle=subtitle,
        border_style="blue",
    )


def interactive_select(
    projects: list[ProjectInfo], root_dir: Path, target_edition: str, branch: str | None
) -> list[ProjectInfo]:
    """Runs the TUI loop for Stage 2 project selection."""
    if not projects:
        console.print(
            "[yellow]No Cargo.toml projects found in the specified directory.[/yellow]"
        )
        return []

    cursor = 0
    with Live(
        render_selection_table(projects, cursor, root_dir, target_edition, branch),
        console=console,
        auto_refresh=False,
    ) as live:
        while True:
            key = get_key()
            if key == "UP":
                cursor = (cursor - 1) % len(projects)
            elif key == "DOWN":
                cursor = (cursor + 1) % len(projects)
            elif key == "SPACE":
                projects[cursor].selected = not projects[cursor].selected
            elif key == "ALL":
                any_unselected = any(not p.selected for p in projects)
                for p in projects:
                    p.selected = any_unselected
            elif key == "ENTER":
                break
            elif key == "QUIT":
                console.print("\n[red]Operation cancelled by user.[/red]")
                sys.exit(0)

            live.update(
                render_selection_table(
                    projects, cursor, root_dir, target_edition, branch
                ),
                refresh=True,
            )

    return [p for p in projects if p.selected]


def run_cmd(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """Executes a subprocess command and returns (success, stderr)."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        return False, err.splitlines()[-1] if err else "Unknown error"
    return True, ""


def create_or_checkout_branch(project_dir: Path, branch_name: str) -> tuple[bool, str]:
    """Creates a new git branch or checks it out if it already exists."""
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return run_cmd(["git", "checkout", branch_name], project_dir)
    return run_cmd(["git", "checkout", "-b", branch_name], project_dir)


def migrate_single_project(
    proj: ProjectInfo, target_edition: str, branch: str | None, lock: threading.Lock
):
    """Worker function for Stage 3 parallel migration."""

    def set_state(state: str, color: str, error: str = ""):
        with lock:
            proj.run_state = state
            proj.run_color = color
            if error:
                proj.error_detail = error

    # Step 0: Create/checkout git branch if requested
    if branch:
        set_state(f"git checkout {branch}...", "cyan")
        ok, err = create_or_checkout_branch(proj.path, branch)
        if not ok:
            set_state("FAILED (git branch)", "bold red", err)
            return

    # Step 1: cargo update
    set_state("cargo update...", "cyan")
    ok, err = run_cmd(["cargo", "update"], proj.path)
    if not ok:
        set_state("FAILED (cargo update)", "bold red", err)
        return

    # Step 2: cargo fix --edition
    set_state("cargo fix --edition...", "cyan")
    run_cmd(["cargo", "fix", "--edition", "--allow-dirty", "--allow-staged"], proj.path)

    # Step 3: Update Cargo.toml
    set_state("updating Cargo.toml...", "cyan")
    cargo_toml = proj.path / "Cargo.toml"
    try:
        content = cargo_toml.read_text(encoding="utf-8")
        if EDITION_PATTERN.search(content):
            new_content = EDITION_PATTERN.sub(
                rf'\g<1>"{target_edition}"', content, count=1
            )
        else:
            new_content = content.replace(
                "[package]", f'[package]\nedition = "{target_edition}"', 1
            )
        cargo_toml.write_text(new_content, encoding="utf-8")
    except Exception as e:
        set_state("FAILED (Cargo.toml)", "bold red", str(e))
        return

    # Step 4: cargo test --no-run
    set_state("cargo test --no-run...", "yellow")
    ok, err = run_cmd(["cargo", "test", "--no-run"], proj.path)
    if not ok:
        set_state("FAILED (build/tests)", "bold red", err)
        return

    # Step 5: cargo fmt
    set_state("cargo fmt...", "cyan")
    run_cmd(["cargo", "fmt"], proj.path)

    set_state("SUCCESS", "bold green")


def render_execution_table(projects: list[ProjectInfo], lock: threading.Lock) -> Panel:
    """Renders the Stage 3 live parallel execution table."""
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("Project", style="bold", width=25)
    table.add_column("Status", width=26)
    table.add_column("Details / Error", style="dim")

    with lock:
        for proj in projects:
            table.add_row(
                proj.name,
                Text(proj.run_state, style=proj.run_color),
                proj.error_detail,
            )

    done = sum(1 for p in projects if p.run_state.startswith(("SUCCESS", "FAILED")))
    return Panel(
        table,
        title=f"[bold]Stage 3: Parallel Migration Progress ({done}/{len(projects)})[/bold]",
        border_style="green",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Interactive TUI Rust Edition Updater."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to search for Rust projects (default: current directory)",
    )
    parser.add_argument(
        "--edition",
        default="2024",
        help="Target Rust edition (default: 2024)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Create/checkout a separate Git branch before updating each project",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 4),
        help="Number of parallel workers (default: up to 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only run Stage 1 scan and exit without interactive execution",
    )
    args = parser.parse_args()

    root_dir = Path(args.directory).resolve()
    if not root_dir.is_dir():
        console.print(
            f"[bold red]Error:[/bold red] Directory '{root_dir}' does not exist."
        )
        sys.exit(1)

    # Stage 1: Scan
    with console.status(
        "[bold cyan]Scanning directories for Rust projects...[/bold cyan]"
    ):
        projects = scan_projects(root_dir, args.edition)

    if args.dry_run:
        console.print(
            render_selection_table(projects, -1, root_dir, args.edition, args.branch)
        )
        return

    # Stage 2: Interactive TUI Selection
    selected_projects = interactive_select(
        projects, root_dir, args.edition, args.branch
    )
    if not selected_projects:
        console.print("[yellow]No projects selected for migration. Exiting.[/yellow]")
        return

    # Stage 3: Parallel Execution with Live UI
    lock = threading.Lock()
    with Live(
        render_execution_table(selected_projects, lock),
        console=console,
        refresh_per_second=8,
    ) as live:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(
                    migrate_single_project, proj, args.edition, args.branch, lock
                )
                for proj in selected_projects
            ]
            while any(not f.done() for f in futures):
                live.update(render_execution_table(selected_projects, lock))
                threading.Event().wait(0.1)

            for f in as_completed(futures):
                f.result()

        live.update(render_execution_table(selected_projects, lock))

    success_count = sum(1 for p in selected_projects if p.run_state == "SUCCESS")
    fail_count = len(selected_projects) - success_count
    console.print(
        f"\n[bold]Migration finished:[/bold] [green]{success_count} succeeded[/green], [red]{fail_count} failed[/red]."
    )


if __name__ == "__main__":
    main()
