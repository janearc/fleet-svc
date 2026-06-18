import click
import subprocess
from rich.console import Console
from rich.table import Table

from fleet.git_state import DelightdUnavailable, fetch_git_state, roster_name_paths


@click.group(help="manage workstation git repositories")
def git():
    pass


def get_github_url(remote_url, branch):
    if not remote_url:
        return ""
    if remote_url.startswith("git@github.com:"):
        path = remote_url.split("git@github.com:")[1].replace(".git", "")
        return f"https://github.com/{path}/tree/{branch}"
    if remote_url.startswith("https://github.com/"):
        path = remote_url.split("https://github.com/")[1].replace(".git", "")
        return f"https://github.com/{path}/tree/{branch}"
    return remote_url


@git.command(help="show git status across all fleet repositories")
def status():
    try:
        repos = fetch_git_state()
    except DelightdUnavailable as exc:
        click.echo(click.style(f"delightd unreachable, cannot show git state: {exc}", fg="red"))
        return

    console = Console(width=200)
    table = Table(title="Workstation Git State")
    table.add_column("Repository", style="cyan")
    table.add_column("Branch", style="magenta")
    table.add_column("Dirty", justify="center")
    table.add_column("Unpushed", justify="right")
    table.add_column("GitHub URL", style="blue", overflow="fold")

    for repo in repos:
        name = repo.get("name", "")
        branch = repo.get("branch", "")
        if repo.get("error"):
            table.add_row(name, "[red]?[/red]", "[red]?[/red]", "[red]unreadable[/red]", "")
            continue
        dirty = "✓" if repo.get("dirty") else ""
        if not repo.get("has_upstream"):
            unpushed = "No upstream"
        elif repo.get("unpushed", 0) > 0:
            unpushed = str(repo["unpushed"])
        else:
            unpushed = ""
        github_url = get_github_url(repo.get("remote_url", ""), branch)
        table.add_row(name, branch, dirty, unpushed, github_url)

    console.print(table)


@git.command(help="commit and push dirty repositories")
@click.option('--all', 'push_all', is_flag=True, help='Push all dirty/unpushed branches')
@click.option('--repo', help='Push a specific repository')
def push(push_all, repo):
    import os

    try:
        repos = fetch_git_state()
    except DelightdUnavailable as exc:
        click.echo(click.style(f"delightd unreachable, cannot determine what to push: {exc}", fg="red"))
        return

    # roster maps a project name to its path for the actual push action below
    paths = {name: path for name, path in roster_name_paths()}

    repos_to_push = []
    for r in repos:
        if repo and r["name"] != repo:
            continue
        if r.get("error"):
            continue
        if r.get("dirty") or r.get("unpushed", 0) > 0 or not r.get("has_upstream"):
            repos_to_push.append(r)

    if not repos_to_push:
        click.echo(click.style("Nothing to push!", fg="green"))
        return

    if not push_all and not repo:
        click.echo("The following repositories have unpushed or dirty changes:")
        for r in repos_to_push:
            click.echo(f" - {r['name']}")
        click.echo("Run with --all to push everything, or --repo <name> to push one.")
        return

    for r in repos_to_push:
        name = r["name"]
        branch = r.get("branch") or "main"
        repo_path = os.path.expanduser(paths.get(name, f"~/work/{name}"))
        click.echo(f"\nProcessing {name}...")

        if r.get("dirty"):
            click.echo("  Committing dirty changes...")
            subprocess.run(["git", "-C", repo_path, "add", "."])
            subprocess.run(["git", "-C", repo_path, "commit", "-m", "fleet: automated commit of dirty changes"])

        click.echo(f"  Pushing branch {branch}...")
        res = subprocess.run(["git", "-C", repo_path, "push", "-u", "origin", branch])
        if res.returncode == 0:
            click.echo(click.style(f"  ✓ Successfully pushed {name}", fg="green"))
        else:
            click.echo(click.style(f"  ✗ Failed to push {name}", fg="red"))
