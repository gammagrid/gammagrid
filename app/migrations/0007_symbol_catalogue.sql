-- The directory of symbols with listed options, so the box you type a ticker
-- into can be searched instead of guessed at.
--
-- WHY A TABLE AND NOT A FILE IN THE REPOSITORY. The directory is somebody
-- else's data, it changes as companies list and delist, and a copy committed
-- here would be stale in a way nobody could see — a symbol listed last month
-- simply would not be found, and the person searching would conclude the
-- search is broken rather than that the file is old. Downloaded into this
-- table it is current, and it is the installation's own copy: nothing is sent
-- anywhere to obtain it.
--
-- LAZILY, AND THEN WEEKLY. The first process that finds the table empty fills
-- it; after that the worker refreshes it on a weekly cadence, because a
-- directory of 5,300 symbols changes by a handful of rows a week. That is the
-- whole difference from a product that fetches it every night: this one is
-- installed on somebody's own machine with one command, and a daily request to
-- a third party is not something to sign them up for without a reason.
--
-- COMPANY NAMES ARE THE HALF PEOPLE ACTUALLY TYPE. A list of four-letter
-- symbols is not searchable by anybody who does not already know the answer.
CREATE TABLE IF NOT EXISTS option_symbols (
    symbol       TEXT PRIMARY KEY,
    company      TEXT,
    -- When the directory last listed it. A symbol that DISAPPEARS from the file
    -- keeps its row: delisting is an event, and a symbol nobody can collect any
    -- more is still a symbol somebody may search for and needs an answer about.
    last_seen_at TIMESTAMP NOT NULL DEFAULT now()
);

-- The catalogue is read whole, ordered, once per page load and handed to the
-- browser to filter. No index earns its place on 5,300 rows read in full;
-- the primary key covers the only other access, which is "is this symbol
-- listed at all".
