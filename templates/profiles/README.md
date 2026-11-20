# Example diurnal profiles

`example_residential.csv` is an APPROXIMATE residential diurnal profile, digitised by eye from a screenshot of an
InfoWorks ICM "Design" profile. It is NOT survey, standards or measured data: use it only to try the tooling and
replace it with the project's own profile before relying on dynamic results.

- 288 rows at 5 minutes, `Time,Factor`, factors normalised to a mean of 1.0.
- Shape: about 0.32 overnight, slow decline to 0.25 by 05:00, morning peak about 2.7 at 07:30, falling to about 1.0 by 10:30,
  0.8-0.9 through the afternoon, evening peak about 1.5 at 19:00, down to about 0.35 by midnight.
- Import: `wwnet profile import model.gpkg RES templates/profiles/example_residential.csv`, or the ArcGIS tool
  "Import Diurnal Profile". Text pasted from ICM (tab-separated `Time<TAB>Factor`) is accepted too.
- Times must lie within 00:00-23:59 and increase; a trailing 24:00 row must be dropped. Each factor holds until the next row.
