{
  "patcher": {
    "appversion": {
      "architecture": "x64",
      "major": 9,
      "minor": 0,
      "modernui": 1,
      "revision": 5
    },
    "autosave": 0,
    "boxes": [
      {
        "box": {
          "background": 1,
          "bgcolor": [
            0.18,
            0.19,
            0.22,
            1.0
          ],
          "id": "obj-1",
          "maxclass": "panel",
          "mode": 0,
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [
            30.0,
            30.0,
            360.0,
            210.0
          ],
          "presentation": 1,
          "presentation_rect": [
            10.0,
            10.0,
            360.0,
            210.0
          ],
          "rounded": 8
        }
      },
      {
        "box": {
          "id": "obj-2",
          "maxclass": "live.tab",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": [
            "",
            "",
            "float"
          ],
          "parameter_enable": 1,
          "patching_rect": [
            50.0,
            50.0,
            140.0,
            40.0
          ],
          "presentation": 1,
          "presentation_rect": [
            24.0,
            24.0,
            140.0,
            40.0
          ],
          "saved_attribute_attributes": {
            "valueof": {
              "parameter_enum": [
                "one",
                "two"
              ],
              "parameter_initial": [
                0
              ],
              "parameter_initial_enable": 1,
              "parameter_longname": "sombra_smoke_tab",
              "parameter_mmax": 1,
              "parameter_shortname": "tab",
              "parameter_type": 2,
              "parameter_unitstyle": 9
            }
          },
          "varname": "sombra_smoke_tab"
        }
      },
      {
        "box": {
          "id": "obj-3",
          "maxclass": "live.text",
          "numinlets": 1,
          "numoutlets": 2,
          "outlettype": [
            "",
            ""
          ],
          "parameter_enable": 1,
          "patching_rect": [
            210.0,
            50.0,
            60.0,
            22.0
          ],
          "presentation": 1,
          "presentation_rect": [
            180.0,
            26.0,
            60.0,
            24.0
          ],
          "saved_attribute_attributes": {
            "valueof": {
              "parameter_enum": [
                "PING",
                "PING"
              ],
              "parameter_initial": [
                0
              ],
              "parameter_initial_enable": 1,
              "parameter_longname": "sombra_smoke_ping",
              "parameter_mmax": 1,
              "parameter_shortname": "ping",
              "parameter_type": 1,
              "parameter_unitstyle": 0
            }
          },
          "text": "PING",
          "texton": "PING",
          "varname": "sombra_smoke_ping"
        }
      },
      {
        "box": {
          "id": "obj-4",
          "maxclass": "textbutton",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": [
            "",
            "",
            "int"
          ],
          "parameter_enable": 0,
          "patching_rect": [
            50.0,
            110.0,
            90.0,
            26.0
          ],
          "presentation": 1,
          "presentation_rect": [
            24.0,
            80.0,
            90.0,
            26.0
          ],
          "text": "classic",
          "varname": "smoke_classic"
        }
      },
      {
        "box": {
          "id": "obj-5",
          "maxclass": "led",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [
            "int"
          ],
          "patching_rect": [
            210.0,
            110.0,
            24.0,
            24.0
          ],
          "presentation": 1,
          "presentation_rect": [
            260.0,
            26.0,
            24.0,
            24.0
          ]
        }
      },
      {
        "box": {
          "id": "obj-6",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [
            50.0,
            160.0,
            300.0,
            20.0
          ],
          "presentation": 1,
          "presentation_rect": [
            24.0,
            130.0,
            320.0,
            20.0
          ],
          "text": "(readout)"
        }
      },
      {
        "box": {
          "id": "obj-7",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": [
            "",
            "",
            "",
            ""
          ],
          "patching_rect": [
            50.0,
            280.0,
            150.0,
            22.0
          ],
          "text": "v8 sombra_ui.js"
        }
      },
      {
        "box": {
          "id": "obj-8",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [
            50.0,
            340.0,
            170.0,
            22.0
          ],
          "text": "udpsend 127.0.0.1 3353"
        }
      },
      {
        "box": {
          "id": "obj-9",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [
            240.0,
            340.0,
            90.0,
            22.0
          ],
          "text": "print SMOKE"
        }
      },
      {
        "box": {
          "id": "obj-10",
          "maxclass": "newobj",
          "numinlets": 0,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            420.0,
            50.0,
            110.0,
            22.0
          ],
          "text": "udpreceive 3354"
        }
      },
      {
        "box": {
          "id": "obj-11",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 2,
          "outlettype": [
            "",
            ""
          ],
          "patching_rect": [
            420.0,
            90.0,
            90.0,
            22.0
          ],
          "text": "route /alive"
        }
      },
      {
        "box": {
          "id": "obj-12",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            420.0,
            130.0,
            80.0,
            22.0
          ],
          "text": "heartbeat"
        }
      },
      {
        "box": {
          "id": "obj-13",
          "maxclass": "newobj",
          "numinlets": 0,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            560.0,
            50.0,
            110.0,
            22.0
          ],
          "text": "udpreceive 3355"
        }
      },
      {
        "box": {
          "id": "obj-14",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 2,
          "outlettype": [
            "",
            ""
          ],
          "patching_rect": [
            560.0,
            90.0,
            140.0,
            22.0
          ],
          "text": "route /sombra/reply"
        }
      },
      {
        "box": {
          "id": "obj-15",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            560.0,
            130.0,
            100.0,
            22.0
          ],
          "text": "prepend reply"
        }
      },
      {
        "box": {
          "id": "obj-16",
          "maxclass": "newobj",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            "bang"
          ],
          "patching_rect": [
            420.0,
            200.0,
            80.0,
            22.0
          ],
          "text": "metro 2000"
        }
      },
      {
        "box": {
          "id": "obj-17",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            420.0,
            240.0,
            90.0,
            22.0
          ],
          "text": "poll_status"
        }
      },
      {
        "box": {
          "id": "obj-18",
          "maxclass": "newobj",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            "bang"
          ],
          "patching_rect": [
            560.0,
            200.0,
            80.0,
            22.0
          ],
          "text": "metro 1000"
        }
      },
      {
        "box": {
          "id": "obj-19",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            560.0,
            240.0,
            110.0,
            22.0
          ],
          "text": "check_presence"
        }
      },
      {
        "box": {
          "id": "obj-20",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [
            "bang"
          ],
          "patching_rect": [
            420.0,
            320.0,
            70.0,
            22.0
          ],
          "text": "loadbang"
        }
      },
      {
        "box": {
          "id": "obj-21",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            420.0,
            290.0,
            32.0,
            22.0
          ],
          "text": "1"
        }
      },
      {
        "box": {
          "id": "obj-22",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            560.0,
            290.0,
            32.0,
            22.0
          ],
          "text": "1"
        }
      },
      {
        "box": {
          "id": "obj-23",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            210.0,
            90.0,
            50.0,
            22.0
          ],
          "text": "ping"
        }
      },
      {
        "box": {
          "id": "obj-24",
          "maxclass": "newobj",
          "numinlets": 2,
          "numoutlets": 3,
          "outlettype": [
            "bang",
            "bang",
            ""
          ],
          "patching_rect": [
            50.0,
            200.0,
            60.0,
            22.0
          ],
          "text": "sel 0 1"
        }
      },
      {
        "box": {
          "id": "obj-25",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            50.0,
            236.0,
            170.0,
            22.0
          ],
          "text": "script show smoke_classic"
        }
      },
      {
        "box": {
          "id": "obj-26",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            230.0,
            236.0,
            170.0,
            22.0
          ],
          "text": "script hide smoke_classic"
        }
      },
      {
        "box": {
          "id": "obj-27",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            50.0,
            400.0,
            80.0,
            22.0
          ],
          "text": "thispatcher"
        }
      },
      {
        "box": {
          "id": "obj-28",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": [
            "",
            "",
            ""
          ],
          "patching_rect": [
            210.0,
            160.0,
            200.0,
            22.0
          ],
          "text": "route presence_on presence_off"
        }
      },
      {
        "box": {
          "id": "obj-29",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            210.0,
            196.0,
            32.0,
            22.0
          ],
          "text": "1"
        }
      },
      {
        "box": {
          "id": "obj-30",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            250.0,
            196.0,
            32.0,
            22.0
          ],
          "text": "0"
        }
      },
      {
        "box": {
          "id": "obj-31",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": [
            "",
            "",
            ""
          ],
          "patching_rect": [
            420.0,
            400.0,
            120.0,
            22.0
          ],
          "text": "route ears hands"
        }
      },
      {
        "box": {
          "id": "obj-32",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [
            ""
          ],
          "patching_rect": [
            420.0,
            440.0,
            90.0,
            22.0
          ],
          "text": "prepend set"
        }
      }
    ],
    "classnamespace": "box",
    "dependency_cache": [],
    "fileversion": 1,
    "gridsize": [
      15.0,
      15.0
    ],
    "lines": [
      {
        "patchline": {
          "destination": [
            "obj-8",
            0
          ],
          "source": [
            "obj-7",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-9",
            0
          ],
          "source": [
            "obj-7",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-11",
            0
          ],
          "source": [
            "obj-10",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-12",
            0
          ],
          "source": [
            "obj-11",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-7",
            0
          ],
          "source": [
            "obj-12",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-14",
            0
          ],
          "source": [
            "obj-13",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-15",
            0
          ],
          "source": [
            "obj-14",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-7",
            0
          ],
          "source": [
            "obj-15",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-17",
            0
          ],
          "source": [
            "obj-16",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-7",
            0
          ],
          "source": [
            "obj-17",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-19",
            0
          ],
          "source": [
            "obj-18",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-7",
            0
          ],
          "source": [
            "obj-19",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-21",
            0
          ],
          "source": [
            "obj-20",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-22",
            0
          ],
          "source": [
            "obj-20",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-16",
            0
          ],
          "source": [
            "obj-21",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-18",
            0
          ],
          "source": [
            "obj-22",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-23",
            0
          ],
          "source": [
            "obj-3",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-7",
            0
          ],
          "source": [
            "obj-23",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-24",
            0
          ],
          "source": [
            "obj-2",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-25",
            0
          ],
          "source": [
            "obj-24",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-26",
            0
          ],
          "source": [
            "obj-24",
            1
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-27",
            0
          ],
          "source": [
            "obj-25",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-27",
            0
          ],
          "source": [
            "obj-26",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-28",
            0
          ],
          "source": [
            "obj-7",
            1
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-29",
            0
          ],
          "source": [
            "obj-28",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-30",
            0
          ],
          "source": [
            "obj-28",
            1
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-5",
            0
          ],
          "source": [
            "obj-29",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-5",
            0
          ],
          "source": [
            "obj-30",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-31",
            0
          ],
          "source": [
            "obj-7",
            2
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-32",
            0
          ],
          "source": [
            "obj-31",
            0
          ]
        }
      },
      {
        "patchline": {
          "destination": [
            "obj-6",
            0
          ],
          "source": [
            "obj-32",
            0
          ]
        }
      }
    ],
    "originid": "pat-sombra-1",
    "rect": [
      120.0,
      120.0,
      760.0,
      520.0
    ]
  }
}
