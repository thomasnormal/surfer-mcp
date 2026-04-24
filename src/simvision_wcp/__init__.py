"""WCP (Waveform Communication Protocol) server in front of Cadence SimVision.

Lets any WCP-speaking client drive SimVision without knowing Tcl. Re-uses
simvision_mcp.client.SimVisionClient for the SimVision side so Xvfb, the
bootstrap Tcl, and lifecycle handling stay consistent.
"""
