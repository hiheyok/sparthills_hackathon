# 5 minute demo script

The challenge asks for three things in the video: **architecture**, **the application and persona**, and **Bedrock in use**. Judges also need a way to contact the team. This script covers all of them and tells you what each part earns on the rubric.

Read the lines in quotes in your own words. Do not read them word for word.

## Before you record (10 minutes)

1. `bash scripts/deploy.sh`, then `python3 scripts/smoke.py <url>`. It must print `OK`. If it says a stage fell back, fix model access first (README, Troubleshooting) or your video will show the rules fallback instead of Bedrock.
2. Open the live URL in a browser window sized like a phone or a half screen. Use a private window for a clean first view without existing saved lists.
3. Do one full dry run of each example. Model wording changes from run to run, so look at the real result before you talk about it.
4. Have `docs/architecture.png` open in another tab.
5. Write your team name and a contact on the last slide or in the video description.

## Timeline

| Time | Part | Rubric |
|---|---|---|
| 0:00 to 0:40 | The persona and the problem | Real persona and need (20) |
| 0:40 to 1:40 | Architecture | Understanding what you built (15), AWS AI use (20) |
| 1:40 to 3:40 | Live demo of the app | Working app (25), Polish (10) |
| 3:40 to 4:20 | Bedrock in use: the trace panel | AWS AI use (20) |
| 4:20 to 5:00 | Limits, what is next, contact | Above and beyond (10), Understanding (15) |

### 0:00 to 0:40 Persona and problem

Show the app's first screen.

> "Samantha is a family caregiver in San Jose. She receives a wildfire warning from nearby and the power has gone out. She needs to take care of her baby and lacks the necessities. However, her phone has a weak signal and not that much time. She does not know which store has anything she needs."

Say clearly that Samantha is a composite persona and the stores are fictional demo data.

### 0:40 to 1:40 Architecture

Show `architecture.png`. Point to each box as you name it.

> "The browser calls API Gateway, which invokes one Lambda. We understand the request once, then build driving and walking plans together. `engine.py` checks stock, opening hours, quantities and safety, then chooses pickup stops. `ai.py` calls Bedrock Converse to understand the request, propose substitutes, write briefings and answer questions. Rules fallback runs inside that same Lambda."

> "Bedrock is called through the Converse API with a chain of models: Claude Haiku 4.5 first, then Nova 2 Lite, then Nova Lite. If every model fails, the app still works with a rules fallback, and it says so on screen. Neighbor stock reports go to DynamoDB and expire after six hours. Logs go to CloudWatch."

> "Map display uses Google Maps when configured, otherwise Leaflet with OpenStreetMap tiles. Driving and walking paths come from separate OSRM services through our server. The browser queues those calls, including per-store travel previews. Checklist progress and bookmarked supply lists stay on this device."

> "The key safety idea: the model proposes, code disposes. Every item has a policy. Insulin is never substituted. Propane can only become items on a hand-written safe list. The model cannot override that."

### 1:40 to 3:40 Live demo

**Example 1: Samantha's wildfire request** (about 55 seconds)

Type: "There is a wildfire warning nearby and the power is out. I need baby formula, diapers, bottled water and canned food for my baby and me." Select Downtown and press **Enter** or **Find supplies**. The homepage chips are Hurricane, Power outage, Flood and Tornado; Samantha's request is entered directly.

* Show the progress strip while the request runs; it hides once the foreground plan finishes. The persistent evidence is in **Under the hood**.
* Open **Your request** and show the parsed needs. If the result includes an **Inferred** item, explain that the model added it from the situation.
* Open **Pickup route** for the actual stop order, estimated arrivals and opening labels. Explain that selection favors urgent needs, bundled pickups and place types likely to supply the item; stock and safety checks still apply.
* Point at the map's road line and **Directions** link. Explain that map pin numbers and the previous/next store browser mean nearest by straight-line distance, while **Pickup route** shows the planned stop sequence. If the line stays dashed, read the routing failure message rather than assuming a cause.
* Compare Drive and Walk on the store card. Routed distances and times replace labeled estimates when available; these direct-to-store values differ from the whole pickup trip.
* Point at the data age on a stop ("Checked 20 min ago", or "call ahead" when the data is stale).
* If any item has a yellow alternative sign, open it and read the caution text.

**Example 2: Power outage** (about 25 seconds)

Start a **New request**, click **Power outage**, then submit. This fills the storm request for D batteries and camp-stove propane, which are sold out near downtown in the demo data.

* Show an item with no stock, then the alternative and the reason it does the same job.
* Say: "The model could only pick from items confirmed in stock near her."

**Example 3: Gas-smell safety check** (about 20 seconds)

Start a **New request** and type: "Earthquake. We smell gas in the garage and need bottled water and canned food." Submit it; there is no earthquake chip on the current homepage.

* Show the **call 911** banner. Say: "A keyword safety net does this in code, so it works even if the model misses it."

**Follow-up and neighbor report** (about 20 seconds)

* Open **Ask about plan**, type a follow-up such as "Can I use a camping stove indoors?", and read the answer and its source. **Run details** shows model evidence when Bedrock answered.
* Open **Pickup route**, then **Tell neighbors what you find here** on a stop and press a report button. Say reports expire after 6 hours.

Optional if you have time: check off a supply, bookmark the list, and reopen it through **My supply lists**. Turn off your network and reload, then choose **Open saved plan** or a bookmarked list to show saved content. The basemap and fresh stock are not downloaded for offline use; restoring a plan does not update inventory.

### 3:40 to 4:20 Bedrock in use

Scroll to the bottom and open **Under the hood: what Bedrock did** (it is folded away to keep the page short).

> "This table is the proof that Bedrock is doing real work. For each stage it shows whether Bedrock or the rules answered, which model, how long it took, and tokens in and out. Understanding, alternatives and the briefing all ran on Bedrock here."

Optionally show the terminal with `python3 scripts/smoke.py <url>` printing the same table and `OK`.

If a stage shows `rules`, do not hide it. Say what happened and why. Honest is better than a faked pass.

### 4:20 to 5:00 Limits, next steps, contact

> "Honest limits: the stores and stock are fictional, drive times are estimates without live traffic, and neighbor reports are unverified. Next we would connect real inventory, live traffic and road closures, and add SMS access for people with no data."

> "We are [team name]. You can reach us at [contact]."

## If something goes wrong while recording

| Problem | What to do |
|---|---|
| A stage shows `rules` | Run `smoke.py` and read the reason. Usually model access. Fix, then re-record. |
| The model gives an odd answer | Re-run once. If it repeats, pick another example and mention nothing is cherry-picked in the data, only the example choice. |
| Slow first request | The first call after idle can be slow. Do a warm-up run before recording. |
| Blank map tiles | You are offline or tiles are blocked. The route diagram still shows the plan. |
| Dashed straight line on the map | The free road routing service did not answer. Try again, or explain that it falls back on purpose. |

## Questions judges may ask

* **Why not let the model do everything?** Stock, hours and routing must be exact, and a wrong substitute for insulin or formula can hurt someone. Code does the exact parts and limits the model.
* **What if Bedrock is down?** Four models in a chain, then a rules fallback that is labeled as such.
* **How do you stop bad substitutes?** Catalog policy, a prompt, a validator, and a second check in the engine. There are tests for each case we found.
* **Is the data real?** No. It is generated with a fixed seed so the demo is repeatable. The loader can be swapped for a real feed.
* **How would this scale?** DynamoDB and Lambda scale on demand. The first step would be real inventory data and a cache.
