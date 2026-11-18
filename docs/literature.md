# Literature, standard methods and original modeling choices

The project uses **established operations-research and transport-network building blocks** but does **not** claim that its entire configuration—especially the normalized balanced k-selection criterion—is a published, universally accepted standard. The references below explain the foundations and identify what is specific to this implementation.

## Method-to-literature correspondence

| Component in net-distance-ph | Established idea or source | Important qualification |
| --- | --- | --- |
| Weighted graph shortest paths | [Dijkstra (1959)](#references) | Implemented on a particular **undirected road/ferry graph**, whose edge topology and service assumptions require separate validation |
| Geographic OD cost matrix | [ArcGIS Network Analyst OD Cost Matrix](#references) | The project reports **population-pair-weighted representative-site OD**, not the conventional cost between one arbitrary point per zone |
| Network weighted 1-median and p-median | [Hakimi (1964, 1965)](#references) | The implementation's candidate facilities are **snapped demand vertices**, not all continuous positions along network edges |
| Greedy improvement and vertex substitution for median sites | [Teitz and Bart (1968)](#references) | The project uses its own deterministic greedy + up to three 1-swap passes; no guarantee of globally optimal k > 1 |
| Street-network extraction and analysis | [Boeing (2017)](#references) | OSMnx is literature context **not a claim that OSMnx is the implementation used here** |
| Need to distinguish modeled network access from realized accessibility | [Geurs and van Wee (2004)](#references) | Distances lack observed speed, schedules, legal access and travel-demand behavior |
| Full-census denominator and conditional reachable-pair average | [Implemented definitions](population_weighted.md#5-exactly-what-the-town-matrix-measures) | Arithmetic definitions in this codebase, **not** directly claimed as a named published accessibility index |
| Normalized k-selection score | [Implemented selection objective](population_weighted.md#4-connected-component-aware-network-k-medians) | **Project-specific policy**: normalized road-access distortion + normalized extra-site fraction, equal coefficients |
| Province-equivalent aggregation | [Implemented hierarchy and mass accounting](population_weighted.md#6-upper-levels-are-aggregations-not-fresh-routing) | Sums unnormalized pairwise masses; legal HUC vs ICC type is **not inferred** from missing province parents |

## References

1. **Dijkstra, E. W. (1959).** “A note on two problems in connexion with graphs.” *Numerische Mathematik*, 1, 269–271. [https://doi.org/10.1007/BF01386390](https://doi.org/10.1007/BF01386390).
2. **Hakimi, S. L. (1964).** “Optimum Locations of Switching Centers and the Absolute Centers and Medians of a Graph.” *Operations Research*, 12(3), 450–459. [https://doi.org/10.1287/opre.12.3.450](https://doi.org/10.1287/opre.12.3.450).
3. **Hakimi, S. L. (1965).** “Optimum Distribution of Switching Centers in a Communication Network and Some Related Graph Theoretic Problems.” *Operations Research*, 13(3), 462–475. [https://doi.org/10.1287/opre.13.3.462](https://doi.org/10.1287/opre.13.3.462).
4. **Teitz, M. B., and Bart, P. (1968).** “Heuristic Methods for Estimating the Generalized Vertex Median of a Weighted Graph.” *Operations Research*, 16(5), 955–961. [https://doi.org/10.1287/opre.16.5.955](https://doi.org/10.1287/opre.16.5.955).
5. **Boeing, G. (2017).** “OSMnx: New Methods for Acquiring, Constructing, Analyzing, and Visualizing Complex Street Networks.” *Computers, Environment and Urban Systems*, 65, 126–139. [https://doi.org/10.1016/j.compenvurbsys.2017.05.004](https://doi.org/10.1016/j.compenvurbsys.2017.05.004).
6. **Geurs, K. T., and van Wee, B. (2004).** “Accessibility evaluation of land-use and transport strategies: Review and research directions.” *Journal of Transport Geography*, 12(2), 127–140. [https://doi.org/10.1016/j.jtrangeo.2003.10.005](https://doi.org/10.1016/j.jtrangeo.2003.10.005).
7. **Esri.** “Origin-destination cost matrix analysis layer.” *ArcGIS Pro Network Analyst documentation*. [https://pro.arcgis.com/en/pro-app/3.6/help/analysis/networks/network-analyst-solver-types.htm](https://pro.arcgis.com/en/pro-app/3.6/help/analysis/networks/network-analyst-solver-types.htm). Standard software reference, **not** a dependency of this project.
8. **OpenStreetMap Foundation.** “Licence/Attribution Guidelines.” [https://osmfoundation.org/wiki/Licence/Attribution_Guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines). Data citation and ODbL obligations, **not** an endorsement of this model.

## Source-specific acknowledgments

- **Philippine Statistics Authority:** Q2 2026 Philippine Standard Geographic Code, normalized administrative hierarchy, and 2024 barangay population counts (record exact bundled file hashes).
- **OpenStreetMap contributors:** road and ferry geometries, typically obtained from a pinned [Geofabrik](https://download.geofabrik.de/asia/philippines.html) extract. OSM data are not a ferry operating timetable and do not guarantee legal road access.
- **OCHA/HDX or the pinned Faeldon derivative:** polygon geometry for **representative point** construction, according to the recorded policy. Document the chosen geometry snapshot and code correspondence; do not present an interior point as a measured residential centroid.

### Citing this software in research

Cite an immutable **code commit**, data/source fingerprints, geometry policy, parameter settings, and the exact generated `manifest.json` alongside the relevant methodological literature. A paper about the full method must explicitly name the *project-defined balanced k selector* and distinguish conditional representative-site distances from conventional centroid skims and empirical trip distances.
