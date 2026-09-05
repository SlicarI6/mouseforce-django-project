
HEAD
const path = require('path');

module.exports = {
  mode: 'development',
  entry: path.resolve(__dirname, 'index.js'), // Only the relative path from webpack.config.js
  output: {
    path: path.resolve(__dirname, 'static'), // Output to 'frontend/static' or wherever you want
    filename: 'bundle.js',
  },
  module: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env', '@babel/preset-react'], // Adăugăm presetul React
          },
        },
      },
    ],
  },
  resolve: {
    extensions: ['.js'],
  },
};

const path = require('path');

module.exports = {
  mode: 'development',
  entry: path.resolve(__dirname, 'index.js'), // Only the relative path from webpack.config.js
  output: {
    path: path.resolve(__dirname, 'static'), // Output to 'frontend/static' or wherever you want
    filename: 'bundle.js',
  },
  module: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env', '@babel/preset-react'], // Adăugăm presetul React
          },
        },
      },
    ],
  },
  resolve: {
    extensions: ['.js'],
  },
};
