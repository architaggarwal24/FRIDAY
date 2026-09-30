/**
 * F.R.I.D.A.Y. — ui/webpack.config.js
 */

const path = require("path");
const HtmlWebpackPlugin = require("html-webpack-plugin");
const CopyWebpackPlugin = require("copy-webpack-plugin");

module.exports = {
  mode: "production",
  devtool: false,

  entry: path.join(__dirname, "renderer", "index.jsx"),

  output: {
    path: path.join(__dirname, "renderer", "dist"),
    filename: "bundle.js",
    chunkFilename: "[name].[contenthash:8].js",
  },

  target: "web",

  resolve: {
    extensions: [".js", ".jsx"],
  },

  module: {
    rules: [
      {
        test: /\.(js|jsx)$/,
        exclude: /node_modules/,
        use: {
          loader: "babel-loader",
          options: {
            presets: ["@babel/preset-env", "@babel/preset-react"],
          },
        },
      },
      {
        test: /\.css$/,
        use: ["style-loader", "css-loader"],
      },
      {
        // @fontsource/jetbrains-mono's CSS references these via url() —
        // webpack 5's built-in asset modules handle bundling them,
        // no extra loader package needed.
        test: /\.(woff|woff2|eot|ttf|otf)$/,
        type: "asset/resource",
      },
    ],
  },

  plugins: [
    new HtmlWebpackPlugin({
      template: path.join(__dirname, "renderer", "index.html"),
      filename: path.join(__dirname, "renderer", "dist", "index.html"),
    }),
    new CopyWebpackPlugin({
      patterns: [
        {
          // @mediapipe/tasks-vision's own WASM + glue-JS files, copied
          // as plain files (not resolved as webpack modules — they're
          // fetched at runtime by FilesetResolver itself, and a
          // directory was never a valid target for module resolution
          // in the first place). Comes from the npm package, so it's
          // present once `npm install` has run — but noErrorOnMissing
          // for the same reason as the .task file below: the posture
          // watch is opt-in and off by default, and a problem specific
          // to it (an unexpected package version, say) shouldn't block
          // building the rest of the app.
          from: path.join(__dirname, "node_modules", "@mediapipe", "tasks-vision", "wasm"),
          to: "mediapipe-wasm",
          noErrorOnMissing: true,
        },
        {
          // The face-landmarker .task model has no npm distribution —
          // it must be downloaded once and placed by hand (see
          // ui/README-assets.md). noErrorOnMissing so a fresh checkout
          // without it yet still builds everything else; the posture
          // watch itself is opt-in and off by default, and fails with
          // a clear, caught error rather than blocking the whole build.
          from: path.join(__dirname, "renderer", "assets", "face_landmarker.task"),
          to: "face_landmarker.task",
          noErrorOnMissing: true,
        },
      ],
    }),
  ],
};