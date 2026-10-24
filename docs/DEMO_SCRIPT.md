# 5 minute demo script

The challenge asks for three things in the video: **architecture**, **the application and persona**, and **Bedrock in use**. Judges also need a way to contact the team. This script covers all of them and tells you what each part earns on the rubric.

Read the lines in quotes in your own words. Do not read them word for word.

## Before you record (10 minutes)

1. `bash scripts/deploy.sh`, then `python3 scripts/smoke.py <url>`. It must print `OK`. If it says a stage fell back, fix model access first (README, Troubleshooting) or your video will show the rules fallback instead of Bedrock.
2. Open the live URL in a browser window sized like a phone or a half screen. Clear the browser's site data for the page (or use a private window) so there is no "saved plan" prompt on the first view.
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

> "This is Samantha. A wildfire warning just went out in San José and her power is off. Her dad uses insulin that has to stay cold, and her baby needs formula. She has a weak signal and a few minutes. Search gives her a list of stores. It does not tell her that one closes before she gets there, that D batteries are sold out downtown, or that she must never swap baby formula for something else. SupplyLine does."

Say clearly that Samantha is a composite persona and the stores are fictional demo data.

### 0:40 to 1:40 Architecture

Show `architecture.png`. Point to each box as you name it.

> "One request becomes four stages. The browser calls API Gateway, which invokes one Lambda. The Lambda has two halves. `engine.py` is plain code. It does distance, opening hours at arrival, stock freshness and route planning, because those must be exact and repeatable. `ai.py` calls Amazon Bedrock for the parts that need language and judgment: understanding the request, choosing safe substitutes, and writing the briefing."

> "Bedrock is called through the Converse API with a chain of models: Claude Haiku 4.5 first, then Nova 2 Lite, then Nova Lite. If every model fails, the app still works with a rules fallback, and it says so on screen. Neighbor stock reports go to DynamoDB and expire after six hours. Logs go to CloudWatch."

> "The key safety idea: the model proposes, code disposes. Every item has a policy. Insulin is never substituted. Propane can only become items on a hand-written safe list. The model cannot override that."

### 1:40 to 3:40 Live demo

**Example 1: "Wildfire, insulin, baby"** (about 70 seconds)

Click the chip, then press **Enter** (or click **Find supplies**). Let the progress steps run, since they are real.

* Point at the progress strip under the form: it shows which model handled each step and how long it took.
* Open "What we understood" (it is folded away): it added things Samantha did not say. Show one marked "Inferred".
* Point at the route: stops in order, each with drive minutes, "Arrive about" time and an opening-hours note. Check on your dry run whether the insulin-related stop is first, and if it is, say the order is urgency-weighted on purpose.
* Point at the map: the solid blue line is the fastest road route through the stops, in order, with the drive time and distance below it. The "Turn-by-turn in Google Maps" button opens the whole trip with live traffic. If the map shows a dashed straight line instead, the free routing service was busy: say so and move on.
* Point at the data age on a stop ("Checked 20 min ago", or "call ahead" when the data is stale).
* If any item has a yellow alternative sign, open it and read the caution text.

**Example 3: "Storm outage"** (about 35 seconds)

D batteries and propane are sold out nearby in the demo data, so this is the best example of alternatives.

* Show an item with no stock, then the alternative and the reason it does the same job.
* Say: "The model could only pick from items confirmed in stock near her."

**Example 2: "Earthquake, no water"** (about 25 seconds)

The request mentions a gas smell.

* Show the **call 911** banner. Say: "A keyword safety net does this in code, so it works even if the model misses it."

**Follow-up and neighbor report** (about 20 seconds)

* Type a follow-up such as "Can I use a camping stove indoors?" Read the answer.
* Open "Tell neighbors what you find here" on a stop and press a button. Say reports expire after 6 hours.

Optional if you have time: turn off your network and reload, then press "Open saved plan" to show the last plan working without a connection.

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
