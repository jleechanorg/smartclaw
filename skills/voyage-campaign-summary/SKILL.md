---
name: voyage-campaign-summary
description: Transform a raw Voyage (or any TTRPG campaign) transcript into a Reddit-formatted campaign summary post. Use when the user says "summarize this campaign for Reddit", "write up my Voyage session", "make a campaign post", or shares a transcript with a milestone (e.g., "100 turns", "rank-up", "boss kill").
when_to_use: |
  Use when:
  - User shares a campaign transcript ( Voyage or other TTRPG) and wants a Reddit-formatted summary
  - User mentions reaching a milestone: "100 turns", "level 5", "rank up", etc.
  - User says "post this to Reddit", "write up our session", "campaign summary"
  - User says "I got beta access" after a Voyage session and wants it documented

triggers:
  - voyage campaign summary
  - write up my campaign
  - campaign post
  - reddit campaign
  - summarize our session
  - 100 turns
  - level up post
  - rank up post
  - Voyage session writeup

allowed-tools:
  - terminal
  - read_file
  - search_files

context: |
  ## What this skill does
  
  Takes a raw TTRPG campaign transcript and produces a structured, engaging Reddit-formatted
  summary following the style of posts like r/Voyage/ or r/BaldursGate3/ campaign recaps.
  
  ## Input sources (in priority order)
  
  1. Transcript file path provided by user
  2. Default Voyage stream transcript: ${HOME}/Downloads/voyage_gameplay_stream/transcript_plain.txt
  3. Ask the user for the file path if not provided
  
  ## Output format
  
  Writes to: ~/voyage-campaign-summary.md (or user-specified path)
  Format follows r/Voyage-style post structure:
  
  ## Reddit Post Format (per r/Voyage convention)
  
  ```
  Title: [Campaign Name] — [Milestone] | [System]
  
  Body sections (in order):
  1. Setup / Character Creation (1-2 paragraphs)
  2. The Journey (chronological key beats, 3-8 paragraphs)
  3. Highlights & memorable moments (bulleted list)
  4. System thoughts / GM impressions (1-2 paragraphs)
  5. What's next / hooks for next session (1 paragraph)
  ```
  
  ## Quality bar
  
  - Length: 300-800 words (Reddit-optimal readability)
  - Tone: First-person player POV, conversational, enthusiastic but not breathless
  - Structure: Clear narrative arc from setup → rising action → climax → resolution
  - Detail level: Specific named NPCs, abilities, dice rolls, quotes — not generic
  - Spoiler tags: Wrap major plot twists in >!spoiler text!<

---

# Voyage Campaign Summary — Procedure

## Step 1: Locate the transcript

1. If user provided a file path → use it
2. If user said "Voyage session" → check:
   ```
   ${HOME}/Downloads/voyage_gameplay_stream/transcript_plain.txt
   ```
3. If not found → ask user for the file path

## Step 2: Read the transcript

Read the full transcript. Identify:

**Campaign metadata:**
- System/setting (Voyage, BG3, etc.)
- Campaign name or session number
- Characters (names, classes, brief description)
- Number of players
- GM/system name

**Story arc:**
- Opening situation / quest hook
- Major decision points (3-8 key moments)
- Combat encounters (enemy types, notable abilities used)
- NPC introductions (names, roles)
- Location transitions
- Ending / cliffhanger / resolution

**Memorable moments:**
- Funny player quotes
- Dramatic dice rolls
- Unexpected outcomes
- Impressive character abilities
- Tense standoffs

**System observations:**
- What mechanics worked well
- GM narration style
- World Engine / AI GM specific features (for Voyage: World Engine multi-agent, rank progression, quest boards, inventory UI)
- Any mechanical surprises

## Step 3: Extract structured data

For each section, pull:
- Named NPCs and their roles
- Character abilities used (especially ones that were funny/impressive)
- Dice roll results if mentioned
- Location names
- Rank/level progression
- Quest objectives

## Step 4: Draft the Reddit post

Follow this template:

```
[TITLE — e.g., "Voyage Campaign — Hit 100 Turns Today | First Impressions"]

[1-paragraph setup: who you are, what system, what session/arc]

[The Journey — chronological beats:]
- Beat 1: How the session started, initial situation
- Beat 2: First major decision or encounter
- Beat 3: Mid-session complication or twist
- Beat 4: Climax or most dramatic moment
- Beat 5: Resolution and what's left hanging

[Highlights — bullets:]
* Moment: specific thing that happened, with quote if available
* Moment: another highlight
* System feature: observation about how Voyage handled something

[System Thoughts — 1-2 paragraphs:]
- What stood out about the World Engine (for Voyage)
- How the AI GM handled the session
- Rank/quest system impressions
- What you're looking forward to

[What's Next — 1 paragraph:]
- Open hooks from this session
- What you want to try next
- Questions you have for the community
```

## Step 5: Write the output

1. Write to `~/voyage-campaign-summary.md`
2. If user wants a different path → use that instead
3. Include the raw word count of the source transcript in a comment at the top

## Step 6: Report to user

```
Done. Summary written to: ~/voyage-campaign-summary.md
Source: [filename] — [word count] words
Post length: [X] words
Ready for Reddit. Want me to open it, copy it, or make any revisions?
```

## Quality checklist before finalizing

- [ ] Title is punchy and includes milestone + system
- [ ] Opening paragraph establishes character + situation in 2-3 sentences
- [ ] At least 3 named NPCs mentioned
- [ ] At least 2 specific abilities or mechanics referenced
- [ ] One memorable quote included
- [ ] System observations are specific to Voyage (not generic TTRPG praise)
- [ ] "What's Next" section gives genuine hooks, not generic "to be continued"
- [ ] Word count is 300-800
- [ ] No spoiler tags needed, or they're properly used
