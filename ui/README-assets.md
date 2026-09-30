# One manual asset: the face-landmarker model

The posture watch (`ui/renderer/components/PostureWatch.jsx`) uses
`@mediapipe/tasks-vision`. Its WASM runtime comes straight from the npm
package and is copied into the build automatically by
`CopyWebpackPlugin` (see `webpack.config.js`) — nothing to do there.

The one thing npm doesn't ship is the `.task` model file itself: it's a
~3.6 MB binary that Google distributes separately, not through the
package. It has to be downloaded once and placed by hand:

1. Download `face_landmarker.task` directly:
   ```
   https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
   ```
   (this is the exact URL Google's own `@mediapipe/tasks-vision` examples
   and notebooks use — the "float16" build, ~3.6 MB, smallest of the
   face_landmarker variants. The full model index, if you want a
   different variant, is at:
   https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker#models)

2. Save it to:
   ```
   ui/renderer/assets/face_landmarker.task
   ```
   (create the `assets/` folder if it doesn't exist yet)

3. Run `npm run build` again.

If this file isn't there yet, the build still succeeds — the posture
watch is opt-in and off by default, so its absence doesn't block
anything else. Turning the posture watch on without it just fails with
a caught, visible error (`status === "error"` in PostureWatch.jsx)
rather than crashing or silently doing nothing.

Why this can't be automated: there's no npm package for the model
file, and fetching it from Google's CDN at runtime is exactly what
this app's CSP is written to block (see the comment above the CSP meta
tag in `renderer/index.html`) — loading a model from a remote origin
at runtime is the same "silent nothing happens" failure mode as a
blocked CDN script, which is why everything else in this feature is
bundled locally instead.
