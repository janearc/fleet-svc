import click
import asyncio
from fleet.core import FleetCore
from fleet.display import render_fleet_state, render_pause_result, render_selfcheck

import functools

def coro(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

@click.group()
def main():
    pass

@main.command()
@click.option('--source', default=None, help='Filter by source')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@coro
async def show(source, as_json):
    core = FleetCore()
    state = await core.show(source_filter=source)
    render_fleet_state(state, as_json=as_json)

@main.command()
@click.option('--dry-run', is_flag=True, help='Preview without acting')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@coro
async def pause(dry_run, yes):
    if not dry_run and not yes:
        click.confirm("Are you sure you want to pause non-essential services?", abort=True)
    
    core = FleetCore()
    result = await core.pause(dry_run=dry_run)
    render_pause_result(result)

@main.command()
@click.option('--dry-run', is_flag=True, help='Preview without acting')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@coro
async def resume(dry_run, yes):
    if not dry_run and not yes:
        click.confirm("Are you sure you want to resume paused services?", abort=True)
        
    core = FleetCore()
    result = await core.resume(dry_run=dry_run)
    render_pause_result(result)

@main.command()
@coro
async def selfcheck():
    core = FleetCore()
    sources_health = await core.selfcheck()
    render_selfcheck(sources_health)

@main.command()
@click.option('--port', default=9400, help='HTTP server port')
def serve(port):
    import uvicorn
    from fleet.server import create_app
    from fleet.auth import SSHAuthenticator
    
    core = FleetCore()
    auth = SSHAuthenticator()
    app = create_app(core, auth)
    
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
