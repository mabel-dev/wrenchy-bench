SELECT UserID, SearchPhrase, COUNT(*) FROM {DATASET} GROUP BY UserID, SearchPhrase LIMIT 10;
