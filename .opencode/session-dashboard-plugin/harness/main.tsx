import { render } from "solid-js/web"
import { Dashboard } from "../src/gui/dashboard.js"
import "../src/gui/styles.css"
import { mockSession } from "./mock-session.js"

function App() {
  return (
    <div
      style={{
        width: "360px",
        height: "920px",
        background: "#060a08",
        padding: "0",
        "box-sizing": "border-box",
      }}
    >
      <Dashboard session={mockSession} />
    </div>
  )
}

render(() => <App />, document.getElementById("root")!)
