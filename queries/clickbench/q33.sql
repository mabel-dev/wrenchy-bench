SELECT WatchID, ClientIP, COUNT(*) AS c, SUM(IsRefresh), AVG(ResolutionWidth) FROM {DATASET} GROUP BY WatchID, ClientIP ORDER BY c DESC LIMIT 10;
