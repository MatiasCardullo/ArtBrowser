"""
Example: Extract Following profiles using GraphQL API instead of manual profile opening.

This demonstrates the new workflow:
1. Load Following API template from HAR
2. Extract profiles directly from API response
3. No need to open each profile individually
"""

import json
from pathlib import Path
from scanner import (
    load_following_api_template,
    extract_profiles_from_following_response,
)


def demo_extract_from_har():
    """Extract profiles directly from HAR cached response (for testing)."""
    print("=" * 70)
    print("DEMO: Extract profiles from HAR cache")
    print("=" * 70)
    
    base_dir = Path(".")
    
    # Load API template
    print("\n1. Loading Following API template...")
    template = load_following_api_template(base_dir)
    print(f"   API Path: {template['path']}")
    print(f"   Auth headers: {list(template['headers'].keys())}")
    
    # Load cached response from HAR
    print("\n2. Loading cached Following response from HAR...")
    har_files = sorted(base_dir.glob("x.com_i_api_graphql_Following_Archive*.har"), reverse=True)
    if not har_files:
        print("   ERROR: No Following HAR files found!")
        return
    
    har_path = har_files[0]
    print(f"   Using: {har_path.name}")
    
    har_data = json.loads(har_path.read_text(encoding="utf-8"))
    response = json.loads(har_data["log"]["entries"][0]["response"]["content"]["text"])
    
    # Extract profiles
    print("\n3. Extracting profiles from GraphQL response...")
    profiles = extract_profiles_from_following_response(response)
    
    print(f"\n   ✓ Found {len(profiles)} profiles in single API response!")
    print("\n   Sample profiles:")
    for i, profile in enumerate(profiles[:5], 1):
        print(f"\n   {i}. {profile['title']} (@{profile['detected_handle']})")
        print(f"      URL: {profile['url']}")
        if profile['description']:
            desc_short = profile['description'][:60]
            print(f"      Bio: {desc_short}...")
        if profile['urls']:
            print(f"      Links: {', '.join(profile['urls'][:2])}")
    
    print(f"\n   ... and {len(profiles) - 5} more profiles!")
    
    # Benefits comparison
    print("\n" + "=" * 70)
    print("BENEFITS vs. Manual Profile Opening:")
    print("=" * 70)
    print(f"• Old method: Open {len(profiles)} profiles individually (~1-2 sec each)")
    print(f"             Total time: ~{len(profiles) * 1.5:.0f}-{len(profiles) * 2:.0f} seconds")
    print(f"• New method: Single API call + parse JSON")
    print(f"             Total time: ~1-2 seconds")
    print(f"• Speed improvement: ~{len(profiles)}x faster!")
    print(f"• Data completeness: Same profile info extracted from API response")


def demo_next_cursor():
    """Show how to extract pagination cursor for fetching more profiles."""
    print("\n" + "=" * 70)
    print("DEMO: Pagination with cursor")
    print("=" * 70)
    
    base_dir = Path(".")
    har_files = sorted(base_dir.glob("x.com_i_api_graphql_Following_Archive*.har"), reverse=True)
    if not har_files:
        return
    
    har_data = json.loads(har_files[0].read_text(encoding="utf-8"))
    response = json.loads(har_data["log"]["entries"][0]["response"]["content"]["text"])
    
    # Extract cursor
    try:
        instructions = (
            response.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        for instruction in instructions:
            if instruction.get("type") == "TimelineCursor" and instruction.get("direction") == "Bottom":
                cursor = instruction.get("value", "")
                if cursor:
                    print(f"\nNext cursor for pagination: {cursor[:50]}...")
                    print("Use this to fetch next batch of profiles with same API")
                    return
    except Exception:
        pass
    
    print("\nNo pagination cursor found in response")


if __name__ == "__main__":
    demo_extract_from_har()
    demo_next_cursor()
    
    print("\n" + "=" * 70)
    print("Integration notes for window.py:")
    print("=" * 70)
    print("""
Instead of:
  1. Load Following page
  2. Scroll and extract candidates via HTML parsing
  3. Open each profile individually in workers
  
New approach:
  1. template = load_following_api_template(base_dir)
  2. profiles, next_cursor = fetch_following_profiles_from_api(
         template, user_id, count=100
     )
  3. Use profiles directly without individual profile opens
  4. Optional pagination: use next_cursor for more profiles

This eliminates manual profile opening entirely!
""")
