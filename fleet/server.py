from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from fleet.core import FleetCore
from fleet.auth import SSHAuthenticator
import json

def create_app(core: FleetCore, auth: SSHAuthenticator) -> FastAPI:
    app = FastAPI(title="Fleet Control API")

    async def verify_signature(
        request: Request,
        x_fleet_sig: str = Header(None),
        x_fleet_nonce: str = Header(None)
    ):
        # We need to verify that x_fleet_sig is a valid SSH signature of x_fleet_nonce
        # And the public key used matches one of our trusted keys
        if not x_fleet_sig or not x_fleet_nonce:
            raise HTTPException(status_code=401, detail="Missing auth headers")
            
        # Simplified for now, real implementation needs full signature payload verification
        # The auth module would handle checking if the nonce is active and signature matches
        try:
            # Assuming signature is passed as base64 or similar
            # And we try all trusted keys until one matches
            # public_key_blob = extract_from_request() 
            # if not auth.verify_signature(x_fleet_nonce, x_fleet_sig.encode(), public_key_blob):
            #     raise HTTPException(status_code=403, detail="Invalid signature")
            pass
        except Exception as e:
            raise HTTPException(status_code=403, detail=str(e))
            
        return True

    @app.get("/healthz")
    async def healthz():
        sources_health = await core.selfcheck()
        return {
            "status": "ok",
            "sources": [h.model_dump() for h in sources_health]
        }

    @app.get("/metrics")
    async def metrics():
        # Expose some basic prometheus counters if needed
        # Alternatively, rely on transparent to scrape /api/show and generate metrics
        return JSONResponse(
            content="fleet_up 1\n",
            media_type="text/plain"
        )

    @app.get("/api/show")
    async def show(source: str = None):
        state = await core.show(source_filter=source)
        # Convert datetime to string for json serialization
        return json.loads(state.model_dump_json())

    @app.post("/api/pause", dependencies=[Depends(verify_signature)])
    async def pause(dry_run: bool = False):
        result = await core.pause(dry_run=dry_run)
        return json.loads(result.model_dump_json())

    @app.post("/api/resume", dependencies=[Depends(verify_signature)])
    async def resume(dry_run: bool = False):
        result = await core.resume(dry_run=dry_run)
        return json.loads(result.model_dump_json())

    return app
