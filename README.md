# M2M - Manabox to Moxfield

Copies your Manabox collection to Moxfield: binders, decklists (with formats, folders and commanders) and wish lists. It only goes one way (Manabox -> Moxfield), only adds things, and never deletes anything. It asks before changing anything.

## What you need
- Windows 10 or 11
- Google Chrome
- A Moxfield account
- Your Manabox collection exported as a CSV

## Install
1. Download `M2M.exe` from the [latest release](https://github.com/Twyst/M2M/releases/latest).
2. Double-click it. Windows may warn about an unknown publisher: click **More info**, then **Run anyway**.
3. In Manabox, export your collection as a CSV and choose it at the top of the app.
4. Tick what to sync and click **Start sync**.
5. A Chrome window opens. Log in to Moxfield there once; the app remembers it.

The app controls that Chrome window like a person clicking through Moxfield, so don't close, click in or minimise it. A first sync of a large collection can take from several minutes to over an hour; later syncs only add the differences.

## What it does
- **Collection:** your Manabox binders become Moxfield binders.
- **Decks:** Manabox decks become Moxfield decklists. You pick the format, commander and folder for new decks.
- **Existing decks:** set a format, folder or name, and pick missing commanders.
- **Special cards:** schemes, attractions and similar cards go to their own boards.
- **Lists:** Manabox lists can be merged into your Moxfield wish list or become separate decklists. Moxfield only has one wish list.
- **Review window:** at the end it lists cards that are on Moxfield but not in Manabox.

Settings and your Moxfield login are stored in the `M2M_data` folder next to the program. Delete that folder to reset.

## Feedback
scarlettwyst@gmail.com (subject: M2M feedback)

Find M2M useful? [Tip me on Ko-fi](https://ko-fi.com/twyst)
