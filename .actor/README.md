<!-- .actor/README.md -->
# HMNH: person research for hiring

HMNH ("Hire Me Not Him") researches a person from public professional sources, checks each claim against more than one source and returns a rated report with a source for every claim. You decide what to do with it.

## What it does

- Finds the right person among namesakes using the details you give (location, employers, roles, skills, known links).
- Reads public LinkedIn, Facebook and Instagram profiles through other Apify Actors, and ordinary public web pages.
- Extracts claims with a language model, merges claims that agree and keeps the sources behind each one.
- Rates the person, criterion by criterion, against the requirements you list, and flags weak or contradictory evidence.
- In skill search mode, finds and ranks people who have a skill in a place.

## Input

| Field | Meaning |
| --- | --- |
| `mode` | `person` or `skill_search` |
| `purposeConfirmed` | Must be ticked. Confirms a lawful, professional purpose. |
| `name`, `location`, `employers`, `roles`, `skills`, `links` | Describe the person (person mode) |
| `requirements` | Optional JSON list such as `[{"kind": "skill", "label": "Python", "priority": "must"}]` |
| `skill`, `location`, `limit` | Skill search mode |
| `llmProvider`, `llmApiKey`, `llmModel` | Optional. By default the AI models of your Apify account are used. |

## Output

One dataset item with `mode`, `status` and `result`.

- `status: done`: `result` is the full report with findings, sources and rating.
- `status: needs_input`: several profiles matched. `result` lists the questions and candidate profiles. Run again with the right profile address in `links`.

The same item is stored under the key-value store key `OUTPUT`.

## Cost

The run pays for the Apify Actors it calls (search and profile scrapers) and for the language model. Cost per person depends on how many sources are read. Set a maximum charge on the run to cap the spend.

## Responsible use

Only public professional information is read. A confirmed purpose is required for every run. The report is evidence for a human decision, not a decision, and should not be the only basis for rejecting anyone. Follow the data protection law that applies to you, including informing people where it requires that.
