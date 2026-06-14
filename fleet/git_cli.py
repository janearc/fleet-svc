import click
import os
import subprocess
import json
from rich.console import Console
from rich.table import Table

@click.group()
def git():
    """Manage workstation git repositories."""
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

@git.command()
def status():
    """Show git status across all fleet repositories."""
    report_path = os.path.expanduser("~/work/transparent/REPORT/data.json")
    if not os.path.exists(report_path):
        click.echo(click.style("transparent report not found. Cannot verify dirty state safely.", fg="red"))
        return
        
    with open(report_path, "r") as f:
        data = json.load(f)
        
    console = Console(width=200)
    table = Table(title="Workstation Git State")
    table.add_column("Repository", style="cyan")
    table.add_column("Branch", style="magenta")
    table.add_column("Dirty", justify="center")
    table.add_column("Unpushed", justify="right")
    table.add_column("GitHub URL", style="blue", overflow="fold")
    
    for repo in data.get("Repos", []):
        name = repo.get("Name", "")
        branch = repo.get("Branch", "")
        dirty = "✓" if repo.get("Dirty") else ""
        unpushed = str(repo.get("Unpushed", 0)) if repo.get("Unpushed", 0) > 0 else ""
        
        # We also want to manually check the remote URL if transparent didn't have it
        remote_url = repo.get("RemoteURL", "")
        if not remote_url:
            repo_path = os.path.expanduser(f"~/work/{name}")
            if os.path.exists(os.path.join(repo_path, ".git")):
                try:
                    remote_url = subprocess.check_output(["git", "config", "--get", "remote.origin.url"], cwd=repo_path, text=True, stderr=subprocess.DEVNULL).strip()
                except subprocess.CalledProcessError:
                    pass
                    
        github_url = get_github_url(remote_url, branch)
        
        # Also check if it's pushed at all
        unpushed_status = unpushed
        if branch:
            repo_path = os.path.expanduser(f"~/work/{name}")
            try:
                # Check if branch exists on remote
                remote_branch = subprocess.check_output(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_path, text=True, stderr=subprocess.DEVNULL).strip()
                if not remote_branch:
                    unpushed_status = "No upstream"
            except subprocess.CalledProcessError:
                pass
                
        table.add_row(name, branch, dirty, unpushed_status, github_url)
        
    console.print(table)

@git.command()
@click.option('--all', 'push_all', is_flag=True, help='Push all dirty/unpushed branches')
@click.option('--repo', help='Push a specific repository')
def push(push_all, repo):
    """Commit and push dirty repositories."""
    report_path = os.path.expanduser("~/work/transparent/REPORT/data.json")
    if not os.path.exists(report_path):
        click.echo("Report not found.")
        return
        
    with open(report_path, "r") as f:
        data = json.load(f)
        
    repos_to_push = []
    for r in data.get("Repos", []):
        if repo and r["Name"] != repo:
            continue
        if r.get("Dirty") or r.get("Unpushed", 0) > 0:
            repos_to_push.append(r)
            
    if not repos_to_push:
        click.echo(click.style("Nothing to push!", fg="green"))
        return
        
    if not push_all and not repo:
        click.echo("The following repositories have unpushed or dirty changes:")
        for r in repos_to_push:
            click.echo(f" - {r['Name']}")
        click.echo("Run with --all to push everything, or --repo <name> to push one.")
        return
        
    for r in repos_to_push:
        name = r["Name"]
        branch = r.get("Branch", "main")
        repo_path = os.path.expanduser(f"~/work/{name}")
        click.echo(f"\nProcessing {name}...")
        
        if r.get("Dirty"):
            click.echo("  Committing dirty changes...")
            subprocess.run(["git", "add", "."], cwd=repo_path)
            subprocess.run(["git", "commit", "-m", "fleet: automated commit of dirty changes"], cwd=repo_path)
            
        click.echo(f"  Pushing branch {branch}...")
        res = subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo_path)
        if res.returncode == 0:
            click.echo(click.style(f"  ✓ Successfully pushed {name}", fg="green"))
        else:
            click.echo(click.style(f"  ✗ Failed to push {name}", fg="red"))
