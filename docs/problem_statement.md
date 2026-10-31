Challenge #2

# “ How might AI help air traffic controllers, pilots and ground operations staff make faster and better decisions? ”

Description

Today, when a runway closes, weather delays occur or ground equipment fail unexpectedly,
communications are reliant on human phone calls, manual software updates etc., leading to
cascading delays, excessive holding patterns, and excessive fuel consumption. The future of Air
Traffic Management relies on AI assistants that can autonomously communicate and negotiate
trade-offs across organisational boundaries.

Want to see

A Multi-Agent Communication Framework where ATC AI agents, Pilot AI agents, and ground ops
agents are able to negotiate resolutions to real-time disruptions without human intervention until
a consensus is reached.

ATC agent 🠆 must take care of runway wake separation, taxiway blockage, maximise sector
throughput

Pilot Agent 🠆 Preserve fuel reserves, ensure flight safety, minimise turbulence and protect
passengers’ connections

Ground Agent 🠆 Optimise gate utilisation, avoid ground-crew overtime, minimise turnaround delays

These 3 agents must exchange structured data, evaluate cross-domain constraints, and converge
on an agreed solution. This includes sending structured messages, explicit constraint checking,
traceable consensus log, deterministic non-AI fallback

What to avoid

● Combining 3 roles into a single prompt call; The point is to test multi-agent coordination across
independent domains
● LLM agents talking to each other in endless, unconstrained loops with no convergence logic /
turn limits
● Disregard of safety margins; AI agents should not be allowed to accept solutions that ignore
operational constraints

Provided data is in data/

● File in Challenge #2\DATA
● Download this, pick any row with WEATHER_DELAY value of > 15, or LATE_AIRCRAFT_DELAY > 20
● Pass the parameters in that row into the 3 AI Agents
● Demonstrate how agents negotiate revised arrival time, gate assignment, or flight plan
adjustment in under 3 turns