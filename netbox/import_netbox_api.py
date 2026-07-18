#!/usr/bin/env python3
"""Import power_plan.yaml data into NetBox 4.6 via REST API.

This importer is intentionally idempotent for core objects:
- sites
- locations
- manufacturers
- device roles
- device types
- racks
- devices
- power panels
- power feeds
- cables

Cable handling notes:
- device terminations are created as power ports when missing
- power panel terminations are created as power feeds when missing
- unknown endpoint devices are auto-created as Generic devices
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, Optional

import requests
import yaml


class NetBoxError(RuntimeError):
    pass


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"(^-+|-+$)", "", value)
    return value or "item"


class NetBoxClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.verify_ssl = verify_ssl
        self.dry_run = dry_run

    def _url(self, endpoint: str) -> str:
        endpoint = endpoint.strip("/")
        return f"{self.base_url}/api/{endpoint}/"

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.session.get(self._url(endpoint), params=params or {}, verify=self.verify_ssl, timeout=30)
        if not resp.ok:
            raise NetBoxError(f"GET {endpoint} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            print(f"[DRY-RUN] POST /api/{endpoint}/ {json.dumps(payload, ensure_ascii=False)}")
            return {"id": -1, **payload}
        resp = self.session.post(self._url(endpoint), data=json.dumps(payload, ensure_ascii=False), verify=self.verify_ssl, timeout=30)
        if not resp.ok:
            raise NetBoxError(f"POST {endpoint} failed: {resp.status_code} {resp.text}\nPayload: {json.dumps(payload, ensure_ascii=False)}")
        return resp.json()

    def patch(self, endpoint: str, obj_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            print(f"[DRY-RUN] PATCH /api/{endpoint}/{obj_id}/ {json.dumps(payload, ensure_ascii=False)}")
            return {"id": obj_id, **payload}
        resp = self.session.patch(
            self._url(endpoint) + f"{obj_id}/",
            data=json.dumps(payload, ensure_ascii=False),
            verify=self.verify_ssl,
            timeout=30,
        )
        if not resp.ok:
            raise NetBoxError(f"PATCH {endpoint}/{obj_id} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def find_one(self, endpoint: str, **filters: Any) -> Optional[Dict[str, Any]]:
        data = self.get(endpoint, params={**filters, "limit": 1})
        results = data.get("results", [])
        return results[0] if results else None

    def ensure(
        self,
        endpoint: str,
        lookup: Dict[str, Any],
        payload: Dict[str, Any],
        update_existing: bool = False,
    ) -> Dict[str, Any]:
        existing = self.find_one(endpoint, **lookup)
        if existing:
            if update_existing:
                patch_data = {}
                for key, value in payload.items():
                    current = existing.get(key)
                    if current != value:
                        patch_data[key] = value
                if patch_data:
                    updated = self.patch(endpoint, existing["id"], patch_data)
                    print(f"Updated {endpoint} id={existing['id']} ({lookup})")
                    return updated
            print(f"Exists  {endpoint} id={existing['id']} ({lookup})")
            return existing

        created = self.post(endpoint, payload)
        print(f"Created {endpoint} id={created.get('id')} ({lookup})")
        return created


def require(mapping: Dict[str, int], key: str, obj_type: str) -> int:
    if key not in mapping:
        raise NetBoxError(f"Missing {obj_type} reference: {key}")
    return mapping[key]


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def find_location_id_by_name(location_ids: Dict[str, int], wanted: str) -> Optional[int]:
    wanted_norm = normalize_name(wanted)
    for name, loc_id in location_ids.items():
        if normalize_name(name) == wanted_norm:
            return loc_id
    return None


def ensure_generic_device_type(
    nb: NetBoxClient,
    manufacturer_ids: Dict[str, int],
    device_type_ids: Dict[str, int],
    update_existing: bool,
) -> int:
    if "Generic" in device_type_ids:
        return device_type_ids["Generic"]
    payload = {
        "manufacturer": require(manufacturer_ids, "Generic", "manufacturer"),
        "model": "Generic",
        "slug": "generic",
        "u_height": 0,
        "is_full_depth": False,
    }
    lookup = {"manufacturer_id": payload["manufacturer"], "model": payload["model"]}
    obj = nb.ensure("dcim/device-types", lookup, payload, update_existing=update_existing)
    device_type_ids["Generic"] = obj["id"]
    return obj["id"]


def ensure_device_role(
    nb: NetBoxClient,
    role_ids: Dict[str, int],
    role: str,
    update_existing: bool,
) -> int:
    if role in role_ids:
        return role_ids[role]
    payload = {"name": role.upper(), "slug": slugify(role), "color": "9e9e9e"}
    obj = nb.ensure("dcim/device-roles", {"slug": slugify(role)}, payload, update_existing=update_existing)
    role_ids[role] = obj["id"]
    return obj["id"]


def ensure_manufacturer(
    nb: NetBoxClient,
    manufacturer_ids: Dict[str, int],
    name: str,
    update_existing: bool,
) -> int:
    if name in manufacturer_ids:
        return manufacturer_ids[name]
    payload = {"name": name, "slug": slugify(name)}
    obj = nb.ensure("dcim/manufacturers", {"name": name}, payload, update_existing=update_existing)
    manufacturer_ids[name] = obj["id"]
    return obj["id"]


def ensure_device_type_for_device(
    nb: NetBoxClient,
    dev_type_name: str,
    manufacturer_ids: Dict[str, int],
    device_type_ids: Dict[str, int],
    device_type_alias_ids: Dict[str, int],
    update_existing: bool,
) -> int:
    existing = device_type_ids.get(dev_type_name)
    if existing is not None:
        return existing

    alias_match = device_type_alias_ids.get(dev_type_name)
    if alias_match is not None:
        device_type_ids[dev_type_name] = alias_match
        return alias_match

    manufacturer_id = ensure_manufacturer(nb, manufacturer_ids, "Generic", update_existing)
    payload = {
        "manufacturer": manufacturer_id,
        "model": dev_type_name,
        "slug": slugify(dev_type_name),
        "u_height": 0,
        "is_full_depth": False,
    }
    lookup = {"manufacturer_id": manufacturer_id, "model": dev_type_name}
    obj = nb.ensure("dcim/device-types", lookup, payload, update_existing=update_existing)
    device_type_ids[dev_type_name] = obj["id"]
    device_type_alias_ids[f"Generic {dev_type_name}"] = obj["id"]
    print(f"Auto-created missing device type: {dev_type_name} (manufacturer: Generic)")
    return obj["id"]


def ensure_device_by_name(
    nb: NetBoxClient,
    device_name: str,
    site_ids: Dict[str, int],
    location_ids: Dict[str, int],
    manufacturer_ids: Dict[str, int],
    device_type_ids: Dict[str, int],
    role_ids: Dict[str, int],
    device_ids: Dict[str, int],
    update_existing: bool,
) -> int:
    if device_name in device_ids:
        return device_ids[device_name]

    site_id = next(iter(site_ids.values()))
    generic_type_id = ensure_generic_device_type(nb, manufacturer_ids, device_type_ids, update_existing)
    pdu_role_id = ensure_device_role(nb, role_ids, "pdu", update_existing)

    payload = {
        "name": device_name,
        "site": site_id,
        "device_type": generic_type_id,
        "role": pdu_role_id,
        "status": "active",
        "description": "Auto-created placeholder for cable termination",
    }

    dashed = device_name.replace("-", " ")
    candidate_location = find_location_id_by_name(location_ids, dashed)
    if candidate_location is not None:
        payload["location"] = candidate_location

    obj = nb.ensure("dcim/devices", {"name": device_name}, payload, update_existing=update_existing)
    device_ids[device_name] = obj["id"]
    return obj["id"]


def ensure_power_port(
    nb: NetBoxClient,
    device_id: int,
    port_name: str,
    update_existing: bool,
) -> int:
    payload = {
        "device": device_id,
        "name": port_name,
        "type": "iec-60320-c14",
        "maximum_draw": None,
        "allocated_draw": None,
        "description": "Auto-created for cable import",
    }
    obj = nb.ensure(
        "dcim/power-ports",
        {"device_id": device_id, "name": port_name},
        payload,
        update_existing=update_existing,
    )
    return obj["id"]


def ensure_power_outlet(
    nb: NetBoxClient,
    device_id: int,
    outlet_name: str,
    update_existing: bool,
) -> int:
    payload = {
        "device": device_id,
        "name": outlet_name,
        "type": "iec-60320-c13",
        "description": "Auto-created for cable import",
    }
    obj = nb.ensure(
        "dcim/power-outlets",
        {"device_id": device_id, "name": outlet_name},
        payload,
        update_existing=update_existing,
    )
    return obj["id"]


def ensure_panel_feed(
    nb: NetBoxClient,
    panel_id: int,
    feed_name: str,
    update_existing: bool,
) -> int:
    payload = {
        "name": feed_name,
        "power_panel": panel_id,
        "status": "active",
        "type": "primary",
        "supply": "ac",
        "description": "Auto-created for cable import",
    }
    obj = nb.ensure(
        "dcim/power-feeds",
        {"power_panel_id": panel_id, "name": feed_name},
        payload,
        update_existing=update_existing,
    )
    return obj["id"]


def resolve_termination(
    nb: NetBoxClient,
    endpoint: Dict[str, Any],
    side: str,
    site_ids: Dict[str, int],
    location_ids: Dict[str, int],
    manufacturer_ids: Dict[str, int],
    role_ids: Dict[str, int],
    device_type_ids: Dict[str, int],
    device_ids: Dict[str, int],
    panel_ids: Dict[str, int],
    update_existing: bool,
) -> Dict[str, Any]:
    if "device" in endpoint:
        device_name = endpoint["device"]
        term_name = endpoint["name"]
        device_id = ensure_device_by_name(
            nb,
            device_name,
            site_ids,
            location_ids,
            manufacturer_ids,
            device_type_ids,
            role_ids,
            device_ids,
            update_existing,
        )
        # NetBox expects directional power cabling: source outlet -> destination port.
        if side == "a":
            power_outlet_id = ensure_power_outlet(nb, device_id, term_name, update_existing)
            return {"object_type": "dcim.poweroutlet", "object_id": power_outlet_id}
        power_port_id = ensure_power_port(nb, device_id, term_name, update_existing)
        return {"object_type": "dcim.powerport", "object_id": power_port_id}

    if "power_panel" in endpoint:
        panel_name = endpoint["power_panel"]
        term_name = endpoint["name"]
        panel_id = require(panel_ids, panel_name, "power_panel")
        feed_id = ensure_panel_feed(nb, panel_id, term_name, update_existing)
        return {"object_type": "dcim.powerfeed", "object_id": feed_id}

    raise NetBoxError(f"Unsupported cable termination format: {endpoint}")


def import_cables(
    nb: NetBoxClient,
    model: Dict[str, Any],
    site_ids: Dict[str, int],
    location_ids: Dict[str, int],
    manufacturer_ids: Dict[str, int],
    role_ids: Dict[str, int],
    device_type_ids: Dict[str, int],
    device_ids: Dict[str, int],
    panel_ids: Dict[str, int],
    update_existing: bool,
) -> None:
    for cable in model.get("cables", []):
        label = cable.get("label", "")
        if not cable.get("a_terminations") or not cable.get("b_terminations"):
            print(f"Skipped cable without both terminations: {label}")
            continue

        a_term = resolve_termination(
            nb,
            cable["a_terminations"][0],
            "a",
            site_ids,
            location_ids,
            manufacturer_ids,
            role_ids,
            device_type_ids,
            device_ids,
            panel_ids,
            update_existing,
        )
        b_term = resolve_termination(
            nb,
            cable["b_terminations"][0],
            "b",
            site_ids,
            location_ids,
            manufacturer_ids,
            role_ids,
            device_type_ids,
            device_ids,
            panel_ids,
            update_existing,
        )

        existing = nb.find_one("dcim/cables", label=label) if label else None
        payload = {
            "label": label,
            "type": cable.get("type", "power"),
            "status": cable.get("status", "connected"),
            "description": cable.get("description", ""),
            "a_terminations": [a_term],
            "b_terminations": [b_term],
        }

        if existing:
            print(f"Exists  dcim/cables id={existing['id']} (label={label})")
            if update_existing:
                patch_payload = {
                    "type": payload["type"],
                    "status": payload["status"],
                    "description": payload["description"],
                }
                nb.patch("dcim/cables", existing["id"], patch_payload)
            continue

        created = nb.post("dcim/cables", payload)
        print(f"Created dcim/cables id={created.get('id')} (label={label})")


def import_data(nb: NetBoxClient, model: Dict[str, Any], update_existing: bool = False) -> None:
    site_ids: Dict[str, int] = {}
    location_ids: Dict[str, int] = {}
    manufacturer_ids: Dict[str, int] = {}
    role_ids: Dict[str, int] = {}
    device_type_ids: Dict[str, int] = {}
    device_type_alias_ids: Dict[str, int] = {}
    device_ids: Dict[str, int] = {}
    panel_ids: Dict[str, int] = {}

    for site in model.get("sites", []):
        obj = nb.ensure("dcim/sites", {"slug": site["slug"]}, site, update_existing=update_existing)
        site_ids[site["name"]] = obj["id"]

    for loc in model.get("locations", []):
        payload = {
            "name": loc["name"],
            "slug": loc.get("slug", slugify(loc["name"])),
            "site": require(site_ids, loc["site"], "site"),
            "description": loc.get("description", ""),
            "status": loc.get("status", "active"),
        }
        obj = nb.ensure("dcim/locations", {"name": payload["name"]}, payload, update_existing=update_existing)
        location_ids[loc["name"]] = obj["id"]

    manufacturer_names = {dt["manufacturer"] for dt in model.get("device_types", [])}
    if any(d.get("device_type") == "Generic" for d in model.get("devices", [])):
        manufacturer_names.add("Generic")

    for name in sorted(manufacturer_names):
        ensure_manufacturer(nb, manufacturer_ids, name, update_existing)

    for dev in model.get("devices", []):
        role = dev.get("role", "other")
        ensure_device_role(nb, role_ids, role, update_existing)

    device_types = list(model.get("device_types", []))
    if any(d.get("device_type") == "Generic" for d in model.get("devices", [])):
        device_types.append(
            {
                "manufacturer": "Generic",
                "model": "Generic",
                "slug": "generic",
                "u_height": 0,
                "is_full_depth": False,
            }
        )

    seen_types = set()
    for dt in device_types:
        key = (dt["manufacturer"], dt["model"])
        if key in seen_types:
            continue
        seen_types.add(key)

        payload = {
            "manufacturer": require(manufacturer_ids, dt["manufacturer"], "manufacturer"),
            "model": dt["model"],
            "slug": dt.get("slug", slugify(dt["model"])),
            "u_height": dt.get("u_height", 0),
            "is_full_depth": dt.get("is_full_depth", False),
        }
        lookup = {"manufacturer_id": payload["manufacturer"], "model": payload["model"]}
        obj = nb.ensure("dcim/device-types", lookup, payload, update_existing=update_existing)
        device_type_ids[dt["model"]] = obj["id"]
        device_type_alias_ids[f"{dt['manufacturer']} {dt['model']}"] = obj["id"]

    for rack in model.get("racks", []):
        payload = {
            "name": rack["name"],
            "site": require(site_ids, rack["site"], "site"),
            "location": require(location_ids, rack["location"], "location"),
            "status": rack.get("status", "active"),
            "type": rack.get("type", "4-post-cabinet"),
            "description": rack.get("description", ""),
        }
        nb.ensure("dcim/racks", {"name": payload["name"]}, payload, update_existing=update_existing)

    for dev in model.get("devices", []):
        dev_type_name = dev["device_type"]
        dev_type_id = ensure_device_type_for_device(
            nb,
            dev_type_name,
            manufacturer_ids,
            device_type_ids,
            device_type_alias_ids,
            update_existing,
        )
        payload = {
            "name": dev["name"],
            "site": require(site_ids, dev["site"], "site"),
            "device_type": dev_type_id,
            "role": require(role_ids, dev.get("role", "other"), "role"),
            "status": dev.get("status", "active"),
            "description": dev.get("description", ""),
        }
        if dev.get("location"):
            payload["location"] = require(location_ids, dev["location"], "location")
        obj = nb.ensure("dcim/devices", {"name": payload["name"]}, payload, update_existing=update_existing)
        device_ids[dev["name"]] = obj["id"]

    for panel in model.get("power_panels", []):
        payload = {
            "name": panel["name"],
            "site": require(site_ids, panel["site"], "site"),
            "location": require(location_ids, panel["location"], "location"),
            "description": panel.get("description", ""),
        }
        obj = nb.ensure("dcim/power-panels", {"name": payload["name"]}, payload, update_existing=update_existing)
        panel_ids[panel["name"]] = obj["id"]

    for feed in model.get("power_feeds", []):
        payload = {
            "name": feed["name"],
            "power_panel": require(panel_ids, feed["power_panel"], "power_panel"),
            "status": feed.get("status", "active"),
            "description": feed.get("description", ""),
            "type": feed.get("type", "primary"),
            "supply": feed.get("supply", "ac"),
        }
        nb.ensure(
            "dcim/power-feeds",
            {"name": payload["name"], "power_panel_id": payload["power_panel"]},
            payload,
            update_existing=update_existing,
        )

    import_cables(
        nb,
        model,
        site_ids,
        location_ids,
        manufacturer_ids,
        role_ids,
        device_type_ids,
        device_ids,
        panel_ids,
        update_existing,
    )


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import power_plan.yaml into NetBox 4.6 via REST API")
    parser.add_argument("--file", default="netbox/power_plan.yaml", help="Path to YAML file")
    parser.add_argument("--netbox-url", default=os.getenv("NETBOX_URL"), help="NetBox base URL")
    parser.add_argument("--token", default=os.getenv("NETBOX_TOKEN"), help="NetBox API token")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--dry-run", action="store_true", help="Print API actions without writing")
    parser.add_argument("--update-existing", action="store_true", help="PATCH existing objects when fields differ")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)

    if not args.netbox_url:
        print("Error: missing --netbox-url or NETBOX_URL", file=sys.stderr)
        return 2
    if not args.token:
        print("Error: missing --token or NETBOX_TOKEN", file=sys.stderr)
        return 2

    with open(args.file, "r", encoding="utf-8") as fh:
        model = yaml.safe_load(fh) or {}

    nb = NetBoxClient(
        base_url=args.netbox_url,
        token=args.token,
        verify_ssl=not args.insecure,
        dry_run=args.dry_run,
    )

    try:
        import_data(nb, model, update_existing=args.update_existing)
    except NetBoxError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    print("\nImport completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
