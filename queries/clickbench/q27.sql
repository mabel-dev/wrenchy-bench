SELECT SearchPhrase FROM {DATASET} WHERE SearchPhrase <> '' ORDER BY EventTime, SearchPhrase LIMIT 10;
