SELECT MobilePhoneModel, COUNT(DISTINCT UserID) AS u FROM {DATASET} WHERE MobilePhoneModel <> '' GROUP BY MobilePhoneModel ORDER BY u DESC LIMIT 10;
